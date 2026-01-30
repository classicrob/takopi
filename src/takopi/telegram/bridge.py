from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, cast

from ..logging import get_logger
from ..markdown import MarkdownFormatter, MarkdownParts
from ..progress import ProgressState
from ..runner_bridge import ExecBridgeConfig, RunningTask, RunningTasks
from ..transport import MessageRef, RenderedMessage, SendOptions, Transport
from ..transport_runtime import TransportRuntime
from ..context import RunContext
from ..model import InputRequestEvent, ResumeToken
from ..session_card import (
    SessionCardState,
    format_badge,
    format_activity_item,
    ACTIVITY_SYMBOLS,
)
from ..scheduler import ThreadScheduler
from ..settings import (
    TelegramFilesSettings,
    TelegramTopicsSettings,
    TelegramTransportSettings,
    UnifiedExperienceSettings,
)
from .client import BotClient
from .render import MAX_BODY_CHARS, prepare_telegram, prepare_telegram_multi
from .types import TelegramCallbackQuery, TelegramIncomingMessage

logger = get_logger(__name__)

__all__ = [
    "CARD_EXPAND_CALLBACK",
    "CARD_PAUSE_CALLBACK",
    "INPUT_ANSWER_PREFIX",
    "INPUT_AUTO_PREFIX",
    "TelegramBridgeConfig",
    "TelegramPresenter",
    "TelegramTransport",
    "build_bot_commands",
    "handle_callback_cancel",
    "handle_callback_input_response",
    "handle_cancel",
    "is_cancel_command",
    "is_input_callback",
    "run_main_loop",
    "send_with_resume",
]

CANCEL_CALLBACK_DATA = "takopi:cancel"
CANCEL_MARKUP = {
    "inline_keyboard": [[{"text": "cancel", "callback_data": CANCEL_CALLBACK_DATA}]]
}
CLEAR_MARKUP = {"inline_keyboard": []}

# Input request callback prefixes
INPUT_ANSWER_PREFIX = "takopi:answer:"
INPUT_AUTO_PREFIX = "takopi:auto:"


def _input_request_markup(request_id: str) -> dict:
    """Build inline keyboard for input request messages."""
    return {
        "inline_keyboard": [
            [{"text": "Answer", "callback_data": f"{INPUT_ANSWER_PREFIX}{request_id}"}],
            [
                {
                    "text": "Let liaison handle",
                    "callback_data": f"{INPUT_AUTO_PREFIX}{request_id}",
                }
            ],
        ]
    }


def _urgency_indicator(urgency: str) -> str:
    """Get visual indicator for urgency level."""
    indicators = {
        "low": "",
        "normal": "",
        "high": "[!] ",
        "critical": "[!!] ",
    }
    return indicators.get(urgency, "")


# Session card callback prefixes
CARD_PAUSE_CALLBACK = "takopi:pause"
CARD_EXPAND_CALLBACK = "takopi:expand"
CARD_CONTINUE_CALLBACK = "takopi:continue"


def _format_elapsed(seconds: float) -> str:
    """Format elapsed time for display."""
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours = int(minutes // 60)
    mins = minutes % 60
    return f"{hours}h {mins}m"


def _build_session_card_markup(
    state: SessionCardState,
    *,
    expanded: bool = False,
) -> dict:
    """Build inline keyboard for session card."""
    rows: list[list[dict]] = []

    # Row 1: Control buttons
    control_row: list[dict] = []
    if state.status == "working":
        control_row.append({
            "text": "\u23F8 Pause",
            "callback_data": CARD_PAUSE_CALLBACK,
        })
        control_row.append({
            "text": "\u2716 Cancel",
            "callback_data": CANCEL_CALLBACK_DATA,
        })
    elif state.status == "waiting_input" and not state.pending_inputs:
        control_row.append({
            "text": "\u25B6 Continue",
            "callback_data": CARD_CONTINUE_CALLBACK,
        })
    elif state.is_complete:
        control_row.append({
            "text": "\u21A9 Resume",
            "callback_data": CARD_CONTINUE_CALLBACK,
        })

    if control_row:
        rows.append(control_row)

    # Row 2+: Input response buttons (max 2 shown)
    for inp in state.pending_inputs[:2]:
        short_id = inp.request_id[-6:] if len(inp.request_id) > 6 else inp.request_id
        rows.append([
            {
                "text": f"\u270F Answer [{inp.source}]",
                "callback_data": f"{INPUT_ANSWER_PREFIX}{inp.request_id}",
            },
            {
                "text": "\u23ED Skip",
                "callback_data": f"{INPUT_AUTO_PREFIX}{inp.request_id}",
            },
        ])

    # Row N: Activity toggle (if there's more to show)
    if state.activity_truncated:
        toggle_text = "\u2195 Show less" if expanded else "\u2195 Show more"
        rows.append([{
            "text": toggle_text,
            "callback_data": CARD_EXPAND_CALLBACK,
        }])

    return {"inline_keyboard": rows}


class TelegramPresenter:
    def __init__(
        self,
        *,
        formatter: MarkdownFormatter | None = None,
        message_overflow: str = "trim",
    ) -> None:
        self._formatter = formatter or MarkdownFormatter()
        self._message_overflow = message_overflow

    def render_progress(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        label: str = "working",
    ) -> RenderedMessage:
        parts = self._formatter.render_progress_parts(
            state, elapsed_s=elapsed_s, label=label
        )
        text, entities = prepare_telegram(parts)
        reply_markup = CLEAR_MARKUP if _is_cancelled_label(label) else CANCEL_MARKUP
        return RenderedMessage(
            text=text,
            extra={"entities": entities, "reply_markup": reply_markup},
        )

    def render_final(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        status: str,
        answer: str,
    ) -> RenderedMessage:
        parts = self._formatter.render_final_parts(
            state, elapsed_s=elapsed_s, status=status, answer=answer
        )
        if self._message_overflow == "split":
            payloads = prepare_telegram_multi(parts, max_body_chars=MAX_BODY_CHARS)
            text, entities = payloads[0]
            extra = {"entities": entities, "reply_markup": CLEAR_MARKUP}
            if len(payloads) > 1:
                followups = [
                    RenderedMessage(
                        text=followup_text,
                        extra={
                            "entities": followup_entities,
                            "reply_markup": CLEAR_MARKUP,
                        },
                    )
                    for followup_text, followup_entities in payloads[1:]
                ]
                extra["followups"] = followups
            return RenderedMessage(text=text, extra=extra)
        text, entities = prepare_telegram(parts)
        return RenderedMessage(
            text=text,
            extra={"entities": entities, "reply_markup": CLEAR_MARKUP},
        )

    def render_input_request(
        self,
        event: InputRequestEvent,
        context_line: str | None = None,
    ) -> RenderedMessage:
        """Render an input request for Telegram display."""
        urgency_prefix = _urgency_indicator(event.urgency)
        header = f"{urgency_prefix}Question from {event.source}"

        body_parts = [event.question]

        if event.options:
            options_text = "\n".join(
                f"  {i + 1}. {opt}" for i, opt in enumerate(event.options)
            )
            body_parts.append(f"\nOptions:\n{options_text}")

        if event.context:
            body_parts.append(f"\n(Context: {event.context})")

        body = "\n".join(body_parts)

        parts = MarkdownParts(header=header, body=body)
        text, entities = prepare_telegram(parts)

        return RenderedMessage(
            text=text,
            extra={
                "entities": entities,
                "reply_markup": _input_request_markup(event.request_id),
            },
        )

    def render_session_card(
        self,
        state: SessionCardState,
        *,
        elapsed_s: float,
        expanded: bool = False,
    ) -> RenderedMessage:
        """Render a unified session card for Telegram display.

        The session card scales from 1 to N agents, showing:
        - Agent badges with status indicators
        - Activity feed (truncated or expanded)
        - Pending input requests inline
        - Control buttons (pause, cancel, expand)

        Args:
            state: The session card state to render
            elapsed_s: Elapsed time in seconds
            expanded: Whether to show expanded activity feed

        Returns:
            RenderedMessage ready to send/edit
        """
        # Build header: badges + status line
        header_parts = []

        # Agent badges row
        if state.badges:
            badges_line = " ".join(format_badge(b) for b in state.badges)
            header_parts.append(badges_line)

        # Status line
        status_parts = []
        status_label = {
            "working": "Working",
            "waiting_input": "Waiting for input",
            "done": "Done",
            "cancelled": "Cancelled",
            "error": "Error",
        }.get(state.status, state.status)
        status_parts.append(status_label)

        if len(state.badges) > 1:
            status_parts.append(f"{len(state.badges)} agents")

        status_parts.append(_format_elapsed(elapsed_s))

        total_steps = sum(b.step_count for b in state.badges)
        if total_steps > 0:
            status_parts.append(f"{total_steps} steps")

        header_parts.append(" \u00b7 ".join(status_parts))  # · separator

        header = "\n".join(header_parts)

        # Build body: activity feed + pending inputs
        body_parts = []

        # Activity feed
        if state.activity_items:
            show_engine = state.is_multi_agent
            activity_lines = []
            for item in state.activity_items:
                activity_lines.append(format_activity_item(item, show_engine=show_engine))
            if state.activity_truncated and not expanded:
                remaining = state.activity_total - len(state.activity_items)
                activity_lines.append(f"... ({remaining} more)")
            body_parts.append("\n".join(activity_lines))

        # Pending inputs section
        if state.pending_inputs:
            input_lines = ["\u2753 Waiting for input:"]  # ❓
            for i, inp in enumerate(state.pending_inputs, 1):
                urgency = _urgency_indicator(inp.urgency)
                source_tag = f"[{inp.source}]" if state.is_multi_agent else ""
                q = inp.question[:80] + "..." if len(inp.question) > 80 else inp.question
                input_lines.append(f"{i}. {urgency}{source_tag} {q}")
            body_parts.append("\n".join(input_lines))

        body = "\n\n".join(body_parts) if body_parts else None

        # Build footer: context + resume
        footer_parts = []
        if state.context_line:
            footer_parts.append(state.context_line)
        if state.resume_line:
            footer_parts.append(state.resume_line)
        if state.error_message:
            footer_parts.append(f"\u26A0 {state.error_message}")  # ⚠

        footer = "\n".join(footer_parts) if footer_parts else None

        # Prepare for Telegram
        parts = MarkdownParts(header=header, body=body, footer=footer)
        text, entities = prepare_telegram(parts)

        # Build markup
        markup = _build_session_card_markup(state, expanded=expanded)

        return RenderedMessage(
            text=text,
            extra={"entities": entities, "reply_markup": markup},
        )


def _is_cancelled_label(label: str) -> bool:
    stripped = label.strip()
    if stripped.startswith("`") and stripped.endswith("`") and len(stripped) >= 2:
        stripped = stripped[1:-1]
    return stripped.lower() == "cancelled"


@dataclass(frozen=True, slots=True)
class TelegramBridgeConfig:
    bot: BotClient
    runtime: TransportRuntime
    chat_id: int
    startup_msg: str
    exec_cfg: ExecBridgeConfig
    session_mode: Literal["stateless", "chat"] = "stateless"
    show_resume_line: bool = True
    voice_transcription: bool = False
    voice_max_bytes: int = 10 * 1024 * 1024
    voice_transcription_model: str = "gpt-4o-mini-transcribe"
    voice_transcription_base_url: str | None = None
    voice_transcription_api_key: str | None = None
    forward_coalesce_s: float = 1.0
    media_group_debounce_s: float = 1.0
    allowed_user_ids: tuple[int, ...] = ()
    files: TelegramFilesSettings = field(default_factory=TelegramFilesSettings)
    chat_ids: tuple[int, ...] | None = None
    topics: TelegramTopicsSettings = field(default_factory=TelegramTopicsSettings)
    unified: UnifiedExperienceSettings = field(default_factory=UnifiedExperienceSettings)


class TelegramTransport:
    def __init__(self, bot: BotClient) -> None:
        self._bot = bot

    @staticmethod
    def _extract_followups(message: RenderedMessage) -> list[RenderedMessage]:
        followups = message.extra.get("followups")
        if not isinstance(followups, list):
            return []
        return [item for item in followups if isinstance(item, RenderedMessage)]

    async def _send_followups(
        self,
        *,
        chat_id: int,
        followups: list[RenderedMessage],
        reply_to_message_id: int | None,
        message_thread_id: int | None,
        notify: bool,
    ) -> None:
        for followup in followups:
            await self._bot.send_message(
                chat_id=chat_id,
                text=followup.text,
                entities=followup.extra.get("entities"),
                parse_mode=followup.extra.get("parse_mode"),
                reply_markup=followup.extra.get("reply_markup"),
                reply_to_message_id=reply_to_message_id,
                message_thread_id=message_thread_id,
                disable_notification=not notify,
            )

    async def close(self) -> None:
        await self._bot.close()

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef | None:
        chat_id = cast(int, channel_id)
        reply_to_message_id: int | None = None
        replace_message_id: int | None = None
        message_thread_id: int | None = None
        notify = True
        if options is not None:
            reply_to_message_id = (
                cast(int, options.reply_to.message_id)
                if options.reply_to is not None
                else None
            )
            replace_message_id = (
                cast(int, options.replace.message_id)
                if options.replace is not None
                else None
            )
            notify = options.notify
            message_thread_id = (
                cast(int | None, options.thread_id)
                if options.thread_id is not None
                else None
            )
        else:
            reply_to_message_id = cast(
                int | None,
                message.extra.get("followup_reply_to_message_id"),
            )
            message_thread_id = cast(
                int | None,
                message.extra.get("followup_thread_id"),
            )
            notify = bool(message.extra.get("followup_notify", True))
        followups = self._extract_followups(message)
        sent = await self._bot.send_message(
            chat_id=chat_id,
            text=message.text,
            entities=message.extra.get("entities"),
            parse_mode=message.extra.get("parse_mode"),
            reply_markup=message.extra.get("reply_markup"),
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
            replace_message_id=replace_message_id,
            disable_notification=not notify,
        )
        if sent is None:
            return None
        if followups:
            await self._send_followups(
                chat_id=chat_id,
                followups=followups,
                reply_to_message_id=reply_to_message_id,
                message_thread_id=message_thread_id,
                notify=notify,
            )
        message_id = sent.message_id
        thread_id = (
            sent.message_thread_id
            if sent.message_thread_id is not None
            else message_thread_id
        )
        return MessageRef(
            channel_id=chat_id,
            message_id=message_id,
            raw=sent,
            thread_id=thread_id,
        )

    async def edit(
        self, *, ref: MessageRef, message: RenderedMessage, wait: bool = True
    ) -> MessageRef | None:
        chat_id = cast(int, ref.channel_id)
        message_id = cast(int, ref.message_id)
        entities = message.extra.get("entities")
        parse_mode = message.extra.get("parse_mode")
        reply_markup = message.extra.get("reply_markup")
        followups = self._extract_followups(message)
        edited = await self._bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=message.text,
            entities=entities,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            wait=wait,
        )
        if edited is None:
            return ref if not wait else None
        if followups:
            reply_to_message_id = cast(
                int | None, message.extra.get("followup_reply_to_message_id")
            )
            message_thread_id = cast(
                int | None, message.extra.get("followup_thread_id")
            )
            notify = bool(message.extra.get("followup_notify", True))
            await self._send_followups(
                chat_id=chat_id,
                followups=followups,
                reply_to_message_id=reply_to_message_id,
                message_thread_id=message_thread_id,
                notify=notify,
            )
        message_id = edited.message_id
        thread_id = (
            edited.message_thread_id
            if edited.message_thread_id is not None
            else ref.thread_id
        )
        return MessageRef(
            channel_id=chat_id,
            message_id=message_id,
            raw=edited,
            thread_id=thread_id,
        )

    async def delete(self, *, ref: MessageRef) -> bool:
        return await self._bot.delete_message(
            chat_id=cast(int, ref.channel_id),
            message_id=cast(int, ref.message_id),
        )


async def send_plain(
    transport: Transport,
    *,
    chat_id: int,
    user_msg_id: int,
    text: str,
    notify: bool = True,
    thread_id: int | None = None,
) -> None:
    reply_to = MessageRef(channel_id=chat_id, message_id=user_msg_id)
    rendered_text, entities = prepare_telegram(MarkdownParts(header=text))
    await transport.send(
        channel_id=chat_id,
        message=RenderedMessage(text=rendered_text, extra={"entities": entities}),
        options=SendOptions(reply_to=reply_to, notify=notify, thread_id=thread_id),
    )


def build_bot_commands(
    runtime: TransportRuntime,
    *,
    include_file: bool = True,
    include_topics: bool = False,
):
    from .commands import build_bot_commands as _build

    return _build(
        runtime,
        include_file=include_file,
        include_topics=include_topics,
    )


def is_cancel_command(text: str) -> bool:
    from .commands import is_cancel_command as _is_cancel_command

    return _is_cancel_command(text)


async def handle_cancel(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    running_tasks: RunningTasks,
    scheduler: ThreadScheduler | None = None,
) -> None:
    from .commands import handle_cancel as _handle_cancel

    await _handle_cancel(cfg, msg, running_tasks, scheduler)


async def handle_callback_cancel(
    cfg: TelegramBridgeConfig,
    query: TelegramCallbackQuery,
    running_tasks: RunningTasks,
    scheduler: ThreadScheduler | None = None,
) -> None:
    from .commands import handle_callback_cancel as _handle_callback_cancel

    await _handle_callback_cancel(cfg, query, running_tasks, scheduler)


async def send_with_resume(
    cfg: TelegramBridgeConfig,
    enqueue: Callable[
        [
            int,
            int,
            str,
            ResumeToken,
            RunContext | None,
            int | None,
            tuple[int, int | None] | None,
            MessageRef | None,
        ],
        Awaitable[None],
    ],
    running_task: RunningTask,
    chat_id: int,
    user_msg_id: int,
    thread_id: int | None,
    session_key: tuple[int, int | None] | None,
    text: str,
) -> None:
    from .loop import send_with_resume as _send_with_resume

    await _send_with_resume(
        cfg,
        enqueue,
        running_task,
        chat_id,
        user_msg_id,
        thread_id,
        session_key,
        text,
    )


async def run_main_loop(
    cfg: TelegramBridgeConfig,
    poller=None,
    *,
    watch_config: bool | None = None,
    default_engine_override: str | None = None,
    transport_id: str | None = None,
    transport_config: TelegramTransportSettings | None = None,
) -> None:
    from .loop import run_main_loop as _run_main_loop

    if poller is None:
        await _run_main_loop(
            cfg,
            watch_config=watch_config,
            default_engine_override=default_engine_override,
            transport_id=transport_id,
            transport_config=transport_config,
        )
    else:
        await _run_main_loop(
            cfg,
            poller=poller,
            watch_config=watch_config,
            default_engine_override=default_engine_override,
            transport_id=transport_id,
            transport_config=transport_config,
        )


def is_input_callback(data: str) -> bool:
    """Check if callback data is for an input request."""
    return data.startswith(INPUT_ANSWER_PREFIX) or data.startswith(INPUT_AUTO_PREFIX)


def parse_input_callback(data: str) -> tuple[str, str] | None:
    """Parse input callback data into (action, request_id).

    Returns:
        Tuple of ("answer" | "auto", request_id) or None if invalid
    """
    if data.startswith(INPUT_ANSWER_PREFIX):
        return ("answer", data[len(INPUT_ANSWER_PREFIX) :])
    if data.startswith(INPUT_AUTO_PREFIX):
        return ("auto", data[len(INPUT_AUTO_PREFIX) :])
    return None


async def handle_callback_input_response(
    cfg: TelegramBridgeConfig,
    query: TelegramCallbackQuery,
    running_tasks: RunningTasks,
    *,
    user_response: str | None = None,
) -> None:
    """Handle a callback for an input request.

    Args:
        cfg: Bridge configuration
        query: The callback query from Telegram
        running_tasks: Dict of running tasks
        user_response: The user's response text (if action was "answer")
    """
    callback_data = query.data
    if callback_data is None:
        return

    parsed = parse_input_callback(callback_data)
    if parsed is None:
        return

    action, request_id = parsed

    # Find the running task with this pending input
    for progress_ref, task in running_tasks.items():
        if request_id in task.pending_inputs:
            pending = task.pending_inputs[request_id]

            if action == "auto":
                # Let the liaison handle it - just acknowledge
                logger.info(
                    "input.callback.auto",
                    request_id=request_id,
                    question=pending.question,
                )
                await cfg.bot.answer_callback_query(
                    callback_query_id=query.id,
                    text="Letting liaison decide...",
                )
                # Remove the pending request without sending a response
                task.pending_inputs.pop(request_id, None)
                return

            if action == "answer":
                if user_response is None:
                    # Prompt user to reply with their answer
                    await cfg.bot.answer_callback_query(
                        callback_query_id=query.id,
                        text="Reply to this message with your answer",
                        show_alert=True,
                    )
                    return

                # Send the response back via callback
                if task.input_response_callback is not None:
                    from ..model import InputResponseEvent

                    response_event = InputResponseEvent(
                        engine="liaison",
                        request_id=request_id,
                        response=user_response,
                        responder="user",
                    )
                    await task.input_response_callback(response_event)

                task.pending_inputs.pop(request_id, None)
                await cfg.bot.answer_callback_query(
                    callback_query_id=query.id,
                    text="Response sent",
                )
                return

    # Request not found
    await cfg.bot.answer_callback_query(
        callback_query_id=query.id,
        text="Request no longer active",
        show_alert=True,
    )
