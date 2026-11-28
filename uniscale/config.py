from __future__ import annotations

import fcntl
import functools
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore

logger = logging.getLogger(__name__)

# The server answers requests on a thread pool and the CLI or BLE daemon may run beside it, so
# every vault write (a JSON read-modify-write, a check-then-append, a rewrite) holds one lock:
# flock on <data root>/.vault.lock, which every process resolves the same way. flock belongs to
# the open file, so threads each opening it exclude each other too; re-entrant within a thread.
# ponytail: one lock for all users' vaults; per-user lock files if writes ever queue noticeably
_held = threading.local()
_lock_file: Optional[str] = None


def _vault_lock_path() -> str:
    global _lock_file
    if _lock_file is None:
        override = os.environ.get("UNISCALE_DB_PATH")
        base = os.path.dirname(os.path.abspath(override)) if override else str(load_config().general.resolved_data_dir)
        os.makedirs(base, exist_ok=True)
        _lock_file = os.path.join(base, ".vault.lock")
    return _lock_file


@contextmanager
def vault_lock():
    depth = getattr(_held, "depth", 0)
    if depth == 0:
        fd = os.open(_vault_lock_path(), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        _held.fd = fd
    _held.depth = depth + 1
    try:
        yield
    finally:
        _held.depth -= 1
        if _held.depth == 0:
            os.close(_held.fd)  # closing releases the lock


def vault_locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with vault_lock():
            return fn(*args, **kwargs)
    return wrapper


def write_atomic(path: str, text: str) -> None:
    """Replaces path with text in one step: readers see the old file or the new one, never part of one."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: str, default):
    """The JSON in path, or default when it's missing. An unreadable file is moved aside
    (path.corrupt-<epoch>) rather than left to be overwritten with default."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError as e:
        aside = f"{path}.corrupt-{int(time.time())}"
        os.replace(path, aside)
        logger.error(f"Unreadable {path} ({e}); kept as {aside}")
        return default


DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/uniscale/config.toml")


class GeneralConfig(BaseModel):
    log_level: str = "INFO"
    data_dir: str = "~/.config/uniscale"
    storage_backend: str = "jsonl"  # "jsonl" or "sqlite"

    @property
    def resolved_data_dir(self) -> Path:
        return Path(os.path.expanduser(self.data_dir)).resolve()

    @property
    def measurements_path(self) -> Path:
        return self.resolved_data_dir / "measurements.jsonl"


class ServerConfig(BaseModel):
    # Loopback by default: expose it through a reverse proxy, or set 0.0.0.0 with api_token.
    host: str = "127.0.0.1"
    port: int = 8088
    external_url: str = ""
    api_token: Optional[str] = None
    enable_mtls: bool = False


class AuthConfig(BaseModel):
    # "single": one shared vault, optionally protected by server.api_token (the default).
    # "multi":  one vault per user under <data_dir>/users/<user>/. Every request must identify a
    #           user, via a token in [auth.tokens] or user_header set by a trusted reverse proxy.
    mode: Literal["single", "multi"] = "single"
    user_header: str = "X-Remote-User"
    # Only requests from these addresses/CIDRs may assert user_header; anyone else could spoof it.
    trusted_proxies: List[str] = Field(default_factory=lambda: ["127.0.0.1", "::1"])
    # Bearer token (or HTTP Basic password) -> user. Several tokens may map to the same user.
    tokens: Dict[str, str] = Field(default_factory=dict)


class SyncConfig(BaseModel):
    remote_hub_url: str = ""
    enable_mtls: bool = False
    client_cert: Optional[str] = None
    client_key: Optional[str] = None
    ca_cert: Optional[str] = None
    api_token: Optional[str] = None


class OwnTracksConfig(BaseModel):
    recorder_url: str = ""
    user: str = ""
    device: str = ""
    username: Optional[str] = None
    password: Optional[str] = None


class YunmaiSettings(BaseModel):
    force_pounds: bool = True


class FitdaysSettings(BaseModel):
    auto_tare: bool = True


class KnownDevice(BaseModel):
    name: str
    address: str
    type: str  # yunmai, fitdays, etekcity, viatom_bp2, omron


class DevicesConfig(BaseModel):
    auto_scan: bool = True
    enable_yunmai: bool = True
    enable_fitdays: bool = True
    enable_vesync: bool = True
    enable_viatom_bp2: bool = True
    enable_omron: bool = True
    yunmai: YunmaiSettings = Field(default_factory=YunmaiSettings)
    fitdays: FitdaysSettings = Field(default_factory=FitdaysSettings)
    known: List[KnownDevice] = Field(default_factory=list)


class AiConfig(BaseModel):
    endpoint: str = "http://localhost:11434"
    model: str = "gemma:latest"
    # The chat assistant's display name (dashboard and app).
    name: str = "Dr. Gemma"
    # None: on only when the config sets an [ai] endpoint, so a fresh install never sends the vault to
    # a model nobody set up. true/false forces it.
    enabled: Optional[bool] = None
    # Let "thinking" models (Gemma 4, Qwen 3, ...) reason before answering. Off by default: on a small
    # local box the hidden reasoning costs several times the tokens and time of the answer itself.
    thinking: bool = False
    # The server's context window per request, in tokens (llama.cpp -c per slot). Sets how much chat
    # history and daily log fit in each request; 16384 or more carries 90 days of the log, else 30.
    context_tokens: int = Field(default=8192, ge=4096)

    @property
    def available(self) -> bool:
        return self.enabled if self.enabled is not None else "endpoint" in self.model_fields_set


def element_key(label: str) -> str:
    """Storage key for a counter element: "D(out)" -> "d_out", "F" -> "f"."""
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


FORMULA_OPS = "+-*/"


def parse_formula(text: str) -> Dict[str, object]:
    """
    "D(out) + D(in) = D" -> {"terms": [("+", "D(out)"), ("+", "D(in)")], "result": "D"}. Operands are
    counter labels or numbers joined by + - * / (no brackets; * and / bind first), so a label can't
    contain one of those operators.
    """
    if text.count("=") != 1:
        raise ValueError("a formula needs exactly one '=', e.g. D(out)+D(in)=D")
    lhs, result = (part.strip() for part in text.split("="))
    if not result or not element_key(result) or len(result) > 20:
        raise ValueError("the name after '=' needs a letter or digit and at most 20 characters")
    parts = [p.strip() for p in re.split(r"([+\-*/])", lhs)]
    operands, ops = parts[0::2], ["+"] + parts[1::2]
    if any(not o for o in operands):
        raise ValueError("each + - * / needs a counter or number on both sides")
    return {"terms": list(zip(ops, operands)), "result": result}


def _number(operand: str) -> Optional[float]:
    try:
        return float(operand)
    except ValueError:
        return None


def evaluate_formula(terms: List[tuple], values: Dict[str, float]) -> Optional[float]:
    """Applies parse_formula's terms to values keyed by element_key; None on division by zero."""
    sums: List[float] = []
    for op, operand in terms:
        v = _number(operand)
        v = values.get(element_key(operand), 0) if v is None else v
        if op in "+-":
            sums.append(v if op == "+" else -v)
        elif op == "*":
            sums[-1] *= v
        elif v == 0:
            return None
        else:
            sums[-1] /= v
    total = round(sum(sums), 2)
    return int(total) if total == int(total) else total


class CounterSet(BaseModel):
    """
    A daily tap counter tile: {"title": "D Tracker", "elements": ["D(out)", "D(in)"]}, or with a formula
    {"title": "D Tracker", "formula": "D(out)+D(in)=D"}: the counters on the left get buttons and the
    name on the right is computed from them each day.
    """
    title: str = Field(min_length=1, max_length=40)
    elements: List[str] = Field(min_length=1, max_length=8)
    formula: Optional[str] = Field(default=None, max_length=120)

    @model_validator(mode="before")
    @classmethod
    def _elements_from_formula(cls, data):
        if isinstance(data, dict) and data.get("formula") and not data.get("elements"):
            terms = parse_formula(str(data["formula"]))["terms"]
            labels = [o for _, o in terms if _number(o) is None]
            data = {**data, "elements": list(dict.fromkeys(labels))}
        return data

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title must not be blank")
        return v.strip()

    @field_validator("elements")
    @classmethod
    def _distinct_elements(cls, v: List[str]) -> List[str]:
        labels = [e.strip() for e in v]
        keys = [element_key(e) for e in labels]
        if any(not k or len(e) > 20 for k, e in zip(keys, labels)):
            raise ValueError("each element needs a letter or digit and at most 20 characters")
        if len(set(keys)) != len(keys):
            raise ValueError("elements must be distinct")
        return labels

    @model_validator(mode="after")
    def _formula_matches_elements(self):
        if not self.formula:
            self.formula = None
            return self
        parsed = parse_formula(self.formula)
        keys = {element_key(e) for e in self.elements}
        unknown = [o for _, o in parsed["terms"] if _number(o) is None and element_key(o) not in keys]
        if unknown:
            raise ValueError(f"the formula uses {', '.join(unknown)}, which isn't one of the counters")
        if element_key(parsed["result"]) in keys:
            raise ValueError("the name after '=' must differ from the counters")
        self.formula = self.formula.strip()
        return self

    @property
    def computed(self) -> Optional[Dict[str, object]]:
        """{"key", "label", "terms"} of the formula's result, or None without a formula."""
        if not self.formula:
            return None
        parsed = parse_formula(self.formula)
        return {"key": element_key(parsed["result"]), "label": parsed["result"], "terms": parsed["terms"]}


class ProfileConfig(BaseModel):
    """
    Per-person inputs for PhenoAge and the ACC/AHA pooled-cohort ASCVD equations.
    Stored per user in profile.json next to their vault (see load_profile).
    """
    name: Optional[str] = Field(default=None, max_length=80)  # shown in the dashboard and used by Dr. Gemma
    date_of_birth: Optional[date] = None
    birth_year: Optional[int] = Field(default=None, ge=1900, le=2100)  # kept in step with date_of_birth
    sex: Optional[Literal["male", "female"]] = None  # ASCVD is not computed until this is set
    smoker: bool = False
    diabetic: bool = False
    bp_treated: bool = False  # taking blood-pressure medication
    counter: Optional[CounterSet] = None  # the daily tap counter tile; none shown when unset

    @field_validator("name")
    @classmethod
    def _blank_name_is_none(cls, v: Optional[str]) -> Optional[str]:
        return (v or "").strip() or None

    @model_validator(mode="after")
    def _birth_year_from_date(self) -> "ProfileConfig":
        if self.date_of_birth:
            if not 1900 <= self.date_of_birth.year <= date.today().year or self.date_of_birth > date.today():
                raise ValueError("date_of_birth must be between 1900 and today")
            self.birth_year = self.date_of_birth.year
        return self

    def age_on(self, day: date) -> Optional[int]:
        """Age in completed years on `day`; from the birth year alone when no full date is set."""
        if self.date_of_birth:
            b = self.date_of_birth
            return day.year - b.year - ((day.month, day.day) < (b.month, b.day))
        return day.year - self.birth_year if self.birth_year else None


class SupplementItem(BaseModel):
    name: str
    dosage: float
    unit: str = "mg"
    category: str = "supplement"
    frequency: str = "daily"
    purpose: str = ""


class SupplementsConfig(BaseModel):
    stack: List[SupplementItem] = Field(default_factory=list)


class LegacyGarminConfig(BaseModel):
    enabled: bool = False
    email: str = ""
    password: str = ""


class LegacyCloudConfig(BaseModel):
    garmin: LegacyGarminConfig = Field(default_factory=LegacyGarminConfig)


class UniscaleConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    devices: DevicesConfig = Field(default_factory=DevicesConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    supplements: SupplementsConfig = Field(default_factory=SupplementsConfig)
    owntracks: OwnTracksConfig = Field(default_factory=OwnTracksConfig)
    legacy_cloud: LegacyCloudConfig = Field(default_factory=LegacyCloudConfig)


EXAMPLE_CONFIG_TOML = """# ==============================================================================
# UniScale Configuration File
# Unified offline-first health bridge & telemetry hub
# ==============================================================================

[general]
# Logging verbosity: DEBUG, INFO, WARNING, ERROR
log_level = "INFO"

# Base directory for local metrics, JSONL files, and ECG recordings
data_dir = "~/.config/uniscale"

# Primary local storage format: "jsonl" or "sqlite"
storage_backend = "jsonl"


[server]
# Interface and port for the self-hosted web dashboard & sync API.
# Loopback by default (put a reverse proxy in front). If you bind 0.0.0.0,
# set api_token too.
host = "127.0.0.1"
port = 8088

# Optional external URL (if hosted behind a reverse proxy / domain)
external_url = "https://health.example.com"

# Optional shared secret. When set, every route except /health requires it,
# as a Bearer token (Android app, daemon) or as the HTTP Basic password (browser).
# api_token = "your-secure-token-here"


[sync]
# Upstream UniScale hub URL (used when the CLI daemon uploads to a remote server)
remote_hub_url = ""

# Optional client certificate for an upstream server that requires one
client_cert = ""
client_key = ""
ca_cert = ""
api_token = ""


[auth]
# "single" (default): one vault, protected by server.api_token if set.
# "multi": one vault per user in <data_dir>/users/<user>/. Authentication is done by your
# reverse proxy (basic auth, OAuth/OIDC, mTLS), which passes the user name in user_header,
# or by per-device tokens below. See docs/multi_user.md.
mode = "single"
user_header = "X-Remote-User"
# Only these addresses may set user_header. Keep server.host on loopback behind the proxy.
trusted_proxies = ["127.0.0.1", "::1"]

# Optional per-device tokens (Bearer, or HTTP Basic password) -> user name.
# [auth.tokens]
# "replace-with-a-long-random-token-1" = "alice"
# "replace-with-a-long-random-token-2" = "alice"
# "replace-with-a-long-random-token-3" = "bob"


[devices]
# Enable automatic BLE discovery of compatible smart health devices
auto_scan = true

# Enable or disable specific hardware drivers
enable_yunmai = true
enable_fitdays = true
enable_vesync = true
enable_viatom_bp2 = true
enable_omron = true

[devices.yunmai]
# Send 0x02 handshake to force physical LED display to pounds instead of kg
force_pounds = true

[devices.fitdays]
auto_tare = true

# Optional: Specific known/paired devices to whitelist or alias
# [[devices.known]]
# name = "Primary Bathroom Scale"
# address = "AA:BB:CC:DD:EE:01"
# type = "yunmai"

# [[devices.known]]
# name = "Desk BP & ECG Cuff"
# address = "AA:BB:CC:DD:EE:02"
# type = "viatom_bp2"


[ai]
# OpenAI-compatible LLM server (Ollama, llama.cpp, ...) for the chat assistant. The prompt carries your whole
# health profile, so keep this on a machine you control.
endpoint = "http://localhost:11434"
model = "gemma:latest"
# thinking = false    # true lets thinking models reason first: slower, sometimes better on hard questions
# context_tokens = 8192  # the server's context per request (llama.cpp -c per slot); 16384+ sends 90 days of daily log


[profile]
# Single-user defaults for PhenoAge / ASCVD. Editing the profile from the dashboard or
# `uniscale profile` saves profile.json next to your data, which takes precedence.
# In multi-user mode these are ignored: each person sets their own profile.
# name = "Alex"
# date_of_birth = 1980-06-15   # or just: birth_year = 1980
# sex = "female"        # "male" or "female"; ASCVD risk is only computed once set
# smoker = false
# diabetic = false
# bp_treated = false    # taking blood-pressure medication


# Daily supplement regimen tracked by the dashboard checklist (optional)
# [[supplements.stack]]
# name = "Vitamin D3"
# dosage = 2000
# unit = "IU"
# category = "vitamin"
# frequency = "morning"
# purpose = "Example entry"


[owntracks]
# Optional OwnTracks Recorder to read locations from. Each finished day, the hub fetches that day's
# points and keeps the places where you stayed an hour or more (the points themselves aren't stored).
# recorder_url = "https://location.example.com/owntracks"   # the Recorder's base URL (…/api/0/… under it)
# user = ""        # Recorder user and device; leave empty if the Recorder has only one of each
# device = ""
# username = ""    # HTTP Basic login, if the Recorder is behind one
# password = ""


[legacy_cloud.garmin]
# Optional legacy sync to Garmin Connect cloud (disabled by default for full privacy)
enabled = false
email = ""
password = ""
"""


def load_config(config_path: Optional[str] = None) -> UniscaleConfig:
    """
    Loads configuration from:
    1. Explicit path argument
    2. UNISCALE_CONFIG environment variable
    3. ./uniscale.toml in current working directory
    4. ~/.config/uniscale/config.toml
    5. Fallback: Default configuration
    """
    candidates = []
    if config_path:
        candidates.append(Path(os.path.expanduser(config_path)))

    env_path = os.environ.get("UNISCALE_CONFIG")
    if env_path:
        candidates.append(Path(os.path.expanduser(env_path)))

    candidates.append(Path("uniscale.toml"))
    candidates.append(Path(DEFAULT_CONFIG_PATH))

    for p in candidates:
        if p.is_file():
            try:
                with open(p, "rb") as f:
                    data = tomllib.load(f)
                logger.info(f"Loaded configuration from {p.resolve()}")
                return UniscaleConfig.model_validate(data)
            except Exception as e:
                logger.warning(f"Failed to parse config file {p}: {e}. Falling back.")

    return UniscaleConfig()


_USER_RE = re.compile(r"[a-z0-9][a-z0-9._@-]{0,127}")

# The user a request (server middleware) or process (CLI --user / UNISCALE_USER) acts for.
current_user: ContextVar[Optional[str]] = ContextVar("uniscale_user", default=None)


class NoUserError(RuntimeError):
    """
    Multi-user mode is on but nothing said which user's vault to use. Deliberately not a
    ValueError, so importers' "bad input file" handlers can't swallow it and report success.
    """


def normalize_user(name: str) -> str:
    """
    Lower-cases and validates a user name before it becomes a directory name.
    Rejects anything that could escape users/ (slashes, leading dots) or is ambiguous (spaces).
    """
    user = (name or "").strip().lower()
    if not _USER_RE.fullmatch(user):
        raise ValueError(
            f"Invalid user name {name!r}: use letters, digits, '.', '_', '-' or '@' "
            "(no spaces or slashes, must not start with '.')"
        )
    return user


def active_user() -> Optional[str]:
    """The request's user if inside one, else UNISCALE_USER (set by the CLI's --user), else None."""
    user = current_user.get() or os.environ.get("UNISCALE_USER")
    return normalize_user(user) if user else None


def vault_db_path(cfg: Optional[UniscaleConfig] = None, user: Optional[str] = None) -> str:
    """
    The one place that decides where measurements are read and written.

    * No user (single-user mode): UNISCALE_DB_PATH if set, else <data_dir>/measurements.jsonl.
    * A user (explicit, from the request, or UNISCALE_USER): <base>/users/<user>/measurements.jsonl,
      where <base> is UNISCALE_DB_PATH's directory if set, else data_dir.
    * Multi-user mode with no user raises NoUserError rather than falling back to a shared vault.

    Every reader/writer falls back to this instead of hardcoding a path, and per-user files
    (supplement stack, upload scratch dirs, ECG recordings) live next to the vault it returns.
    """
    cfg = cfg or load_config()
    override = os.environ.get("UNISCALE_DB_PATH")
    user = normalize_user(user) if user else active_user()
    if user is None:
        if cfg.auth.mode == "multi":
            raise NoUserError("Multi-user mode ([auth] mode = \"multi\"): pass --user <name> or set UNISCALE_USER")
        return override or str(cfg.general.measurements_path)
    base = os.path.dirname(os.path.abspath(override)) if override else str(cfg.general.resolved_data_dir)
    return os.path.join(base, "users", user, "measurements.jsonl")


def profile_path(db_path: Optional[str] = None, cfg: Optional[UniscaleConfig] = None) -> str:
    """profile.json lives next to the vault, so it is per user in multi-user mode."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path or vault_db_path(cfg))), "profile.json")


def load_profile(db_path: Optional[str] = None, cfg: Optional[UniscaleConfig] = None) -> ProfileConfig:
    """
    The profile for the vault at db_path (default: the active user's vault).
    Single-user mode: profile.json overrides [profile] from the config file, field by field.
    Multi-user mode: profile.json only; the config's [profile] is never applied to other people.
    """
    cfg = cfg or load_config()
    merged: Dict[str, object] = {} if cfg.auth.mode == "multi" else cfg.profile.model_dump(exclude_unset=True)
    path = profile_path(db_path, cfg)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                merged.update(json.load(f))
        except (OSError, ValueError) as e:
            logger.warning(f"Ignoring unreadable profile {path}: {e}")
    return ProfileConfig.model_validate(merged)


def save_profile(profile: ProfileConfig, db_path: Optional[str] = None, cfg: Optional[UniscaleConfig] = None) -> ProfileConfig:
    """Writes profile.json next to the vault (atomically) and returns what was saved."""
    write_atomic(profile_path(db_path, cfg), json.dumps(profile.model_dump(mode="json"), indent=2))
    return profile


def generate_default_config_file(target_path: Optional[str] = None) -> str:
    path = Path(os.path.expanduser(target_path or DEFAULT_CONFIG_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(EXAMPLE_CONFIG_TOML, encoding="utf-8")
        logger.info(f"Created default configuration at {path}")
    return str(path)
