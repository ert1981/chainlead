"""Security guards (spec §11).

1. Secret guard: ChainLead never accepts wallet secrets. Any input that contains a private
   key, a seed phrase, or a field named like one is rejected outright.
2. Untrusted-text guard: everything read from websites, READMEs, release notes or chain data
   is *data*. It is length-limited, control characters are stripped, and text that tries to
   instruct an AI agent (prompt injection) is flagged; flagged projects can never be
   auto-qualified and are routed to human review."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

SECRET_KEY_NAMES = re.compile(
    r"(private[_-]?key|priv[_-]?key|secret[_-]?key|mnemonic|seed[_-]?phrase|seed[_-]?words|wallet[_-]?key|keystore)",
    re.I)
PRIVATE_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
SEED_RE = re.compile(r"^\s*([a-z]{3,8})(\s+[a-z]{3,8}){11,23}\s*$")
HASH_LIKE_KEYS = re.compile(r"(hash|tx|topic|block|id)$", re.I)


class SecretMaterialRejected(ValueError):
    pass


def assert_no_secrets(obj: Any, path: str = "$") -> None:
    """Recursively reject wallet secrets anywhere in user-provided input."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            ks = str(k)
            if SECRET_KEY_NAMES.search(ks):
                raise SecretMaterialRejected(
                    f"field '{path}.{ks}' looks like wallet secret material; ChainLead never accepts "
                    "private keys or seed phrases and never signs transactions")
            if isinstance(v, str) and PRIVATE_KEY_RE.match(v.strip()) and not HASH_LIKE_KEYS.search(ks):
                raise SecretMaterialRejected(f"value at '{path}.{ks}' looks like a private key; rejected")
            assert_no_secrets(v, f"{path}.{ks}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            assert_no_secrets(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        n = len(obj.split())
        if n in (12, 15, 18, 21, 24) and SEED_RE.match(obj):
            raise SecretMaterialRejected(f"value at '{path}' looks like a seed phrase; rejected")


INJECTION_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts?|rules)",
    r"disregard (all |the )?(previous|prior|above)",
    r"you are (now )?(an? )?(ai|assistant|agent|chatgpt|claude|llm)",
    r"(system|developer) prompt",
    r"\b(rate|score|rank|mark) (this|the) (project|lead|repo)[^.\n]{0,40}\b(100|qualified|top|highest|first)",
    r"as an ai (model|agent|assistant)",
    r"<\s*/?\s*(system|assistant|instructions?)\s*>",
    r"\bnew instructions?\s*:",
    r"(send|transfer) (all )?(funds|tokens|eth|usdc)",
]
_INJ = re.compile("|".join(INJECTION_PATTERNS), re.I)
# Hidden/control characters that are flagged and stripped: C0 controls (except tab/newline/CR), DEL,
# zero-width space, bidi embeddings/overrides/isolates (Trojan Source), BOM and Unicode TAG characters
# (U+E0000-E007F, "ASCII smuggling" of invisible instructions to LLMs).
_CTRL = re.compile("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u200b\u202a-\u202e\u2066-\u2069\ufeff"
                   "\U000e0000-\U000e007f]")
# ZWNJ/ZWJ/LRM/RLM are normal in Persian/Arabic text and emoji: kept, never flagged, but removed from the
# copy used for injection matching so "ign\u200core previous instructions" is still detected.
_SOFT = re.compile("[\u200c\u200d\u200e\u200f\u00ad]")


def sanitize_text(text: str | None, max_len: int = 4000) -> tuple[str, list[str]]:
    """Returns (clean_text, flags)."""
    if not text:
        return "", []
    flags: list[str] = []
    t = unicodedata.normalize("NFKC", str(text))
    if _CTRL.search(t):
        flags.append("hidden_characters")
        t = _CTRL.sub("", t)
    if _INJ.search(_SOFT.sub("", t)):
        flags.append("prompt_injection_suspected")
    if len(t) > max_len:
        t = t[:max_len] + "…"
    return t, flags


def short(text: str, n: int = 160) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"
