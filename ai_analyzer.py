from __future__ import annotations

import logging
from typing import Optional, Tuple

import anthropic

logger = logging.getLogger(__name__)


class AIAnalyzer:
    """
    Claude-powered call analysis.

    Capabilities
    ────────────
    • Post-call summary
    • Sentiment classification (positive / neutral / negative)
    • Answering-machine detection from call metadata
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def analyze_call(
        self,
        contact_name: Optional[str],
        contact_phone: str,
        duration_seconds: int,
        disposition: Optional[str],
        transcript: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        Returns (summary, sentiment).
        sentiment is one of: positive | neutral | negative
        """
        context_lines = [
            f"Contact: {contact_name or 'Unknown'} ({contact_phone})",
            f"Call duration: {duration_seconds}s",
            f"Agent disposition: {disposition or 'not set'}",
        ]
        if transcript:
            context_lines.append(f"\nCall transcript:\n{transcript}")

        context = "\n".join(context_lines)

        prompt = (
            "You are an AI assistant analyzing a call center interaction.\n\n"
            f"Call details:\n{context}\n\n"
            "Provide:\n"
            "1. A concise 2-3 sentence summary of the call outcome.\n"
            "2. Overall sentiment of the customer interaction.\n\n"
            "Respond in this exact JSON format:\n"
            '{"summary": "...", "sentiment": "positive|neutral|negative"}'
        )

        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()

            import json
            data = json.loads(text)
            return data.get("summary", ""), data.get("sentiment", "neutral")

        except Exception as exc:
            logger.error("AI analysis failed: %s", exc)
            return "Analysis unavailable.", "neutral"

    async def detect_answering_machine(
        self,
        call_duration_before_answer_ms: int,
        early_media_detected: bool,
        dtmf_detected: bool,
    ) -> str:
        """
        Lightweight AMD heuristic using call metadata.
        Returns: 'human' | 'machine' | 'unknown'
        """
        prompt = (
            "You are an Answering Machine Detection (AMD) system.\n\n"
            f"Call metadata:\n"
            f"- Time before answer: {call_duration_before_answer_ms}ms\n"
            f"- Early media detected: {early_media_detected}\n"
            f"- DTMF detected: {dtmf_detected}\n\n"
            "Based on this metadata, classify the call answer as:\n"
            "  'human'   — a real person answered\n"
            "  'machine' — an answering machine or voicemail\n"
            "  'unknown' — cannot determine\n\n"
            "Respond with one word only: human, machine, or unknown."
        )
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=10,
                messages=[{"role": "user", "content": prompt}],
            )
            result = response.content[0].text.strip().lower()
            if result not in ("human", "machine", "unknown"):
                result = "unknown"
            return result
        except Exception as exc:
            logger.error("AMD failed: %s", exc)
            return "unknown"

    async def generate_script_suggestion(
        self,
        contact_name: Optional[str],
        product: str,
        call_history_summary: Optional[str] = None,
    ) -> str:
        """Generate a personalised opening script for the agent."""
        context = f"Product/service: {product}\nContact: {contact_name or 'Unknown'}"
        if call_history_summary:
            context += f"\nPrevious interactions: {call_history_summary}"

        prompt = (
            "Generate a friendly, professional 2-3 sentence opening script for a "
            "call center agent.\n\n"
            f"{context}\n\n"
            "The script should introduce the agent, mention the product briefly, "
            "and invite conversation."
        )
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            logger.error("Script suggestion failed: %s", exc)
            return "Hello, I'm calling about an exciting offer. Do you have a moment to chat?"
