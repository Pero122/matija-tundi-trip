"""Small, dependency-free Apify API client with explicit spending guards.

The client deliberately keeps actor execution asynchronous at the HTTP layer.  A
caller can use :meth:`start_actor` and :meth:`wait_for_run` independently, or
use :meth:`run_actor` for the complete start/wait/download workflow.

Authentication is sent only in the ``Authorization`` header.  Tokens are never
put in URLs or log messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import getpass
import json
import math
import os
import subprocess
import threading
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


APIFY_API_BASE_URL = "https://api.apify.com/v2"
TERMINAL_RUN_STATUSES = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})


class ApifyError(RuntimeError):
    """Base class for all client errors."""


class ApifyConfigurationError(ApifyError):
    """Raised when credentials or safety limits are not configured correctly."""


class ApifyTransportError(ApifyError):
    """Raised when no HTTP response could be obtained."""


class ApifyProtocolError(ApifyError):
    """Raised when Apify returns a successful but structurally invalid response."""


class ApifyHttpError(ApifyError):
    """An unsuccessful response from the Apify API."""

    def __init__(
        self,
        *,
        status_code: int,
        method: str,
        url: str,
        error_type: str | None = None,
        server_message: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.url = url
        self.error_type = error_type
        self.server_message = server_message
        self.retry_after = retry_after
        detail = f" ({error_type})" if error_type else ""
        message = f"Apify API {method} failed with HTTP {status_code}{detail}"
        if server_message:
            message += f": {server_message}"
        if retry_after:
            message += f"; retry after {retry_after}"
        super().__init__(message)


class ApifyCostLimitError(ApifyError):
    """Raised before a run when its charge cap exceeds the monthly remainder."""

    def __init__(
        self,
        *,
        requested_usd: Decimal,
        remaining_usd: Decimal,
        monthly_hard_cap_usd: Decimal,
        monthly_usage_usd: Decimal,
    ) -> None:
        self.requested_usd = requested_usd
        self.remaining_usd = remaining_usd
        self.monthly_hard_cap_usd = monthly_hard_cap_usd
        self.monthly_usage_usd = monthly_usage_usd
        super().__init__(
            "Actor run was not started: requested maxTotalChargeUsd "
            f"{requested_usd} exceeds remaining monthly allowance {remaining_usd} "
            f"(usage {monthly_usage_usd} of hard cap {monthly_hard_cap_usd})"
        )


class ApifyRunTimeoutError(ApifyError):
    """Raised when a run does not reach a terminal state before the timeout."""

    def __init__(self, run_id: str, timeout_seconds: float) -> None:
        self.run_id = run_id
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Apify run {run_id!r} did not finish within {timeout_seconds:g} seconds"
        )


class ApifyRunFailedError(ApifyError):
    """Raised when a run reaches a terminal state other than SUCCEEDED."""

    def __init__(self, run: Mapping[str, Any]) -> None:
        self.run = dict(run)
        run_id = run.get("id", "<unknown>")
        status = run.get("status", "<unknown>")
        status_message = run.get("statusMessage")
        message = f"Apify run {run_id!r} finished with status {status!r}"
        if isinstance(status_message, str) and status_message:
            message += f": {status_message}"
        super().__init__(message)


@dataclass(frozen=True)
class TransportResponse:
    """Transport-neutral HTTP response used by the injectable transport."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class UsagePlan:
    """Current account, plan, limits, and usage returned without normalization."""

    user: dict[str, Any]
    limits: dict[str, Any]

    @property
    def plan(self) -> dict[str, Any]:
        value = self.user.get("plan")
        return value if isinstance(value, dict) else {}

    @property
    def current_usage(self) -> dict[str, Any]:
        value = self.limits.get("current")
        return value if isinstance(value, dict) else {}

    @property
    def account_limits(self) -> dict[str, Any]:
        value = self.limits.get("limits")
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class ApifyRunResult:
    """A completed run plus exact dataset metadata and item dictionaries."""

    run: dict[str, Any]
    dataset: dict[str, Any]
    items: list[dict[str, Any]]

    @property
    def run_metadata(self) -> dict[str, Any]:
        """Explicit alias useful at persistence boundaries."""

        return self.run

    @property
    def dataset_metadata(self) -> dict[str, Any]:
        """Explicit alias useful at persistence boundaries."""

        return self.dataset


Transport = Callable[[Request, float], TransportResponse]
Sleep = Callable[[float], None]
Clock = Callable[[], float]


def load_apify_token(
    *,
    environ: Mapping[str, str] | None = None,
    username: str | None = None,
    keychain_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Load APIFY_TOKEN from macOS Keychain, then fall back to the environment.

    The Keychain item is looked up using service ``APIFY_TOKEN`` and the current
    OS username as its account.  Failures are intentionally silent so callers do
    not accidentally expose credential-related subprocess output.
    """

    account = username or getpass.getuser()
    try:
        completed = keychain_runner(
            [
                "security",
                "find-generic-password",
                "-s",
                "APIFY_TOKEN",
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if completed.returncode == 0:
            token = completed.stdout.strip()
            if token:
                return token
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass

    env = os.environ if environ is None else environ
    token = env.get("APIFY_TOKEN", "").strip()
    if token:
        return token
    raise ApifyConfigurationError(
        "APIFY_TOKEN is unavailable in macOS Keychain and the environment"
    )


def urllib_transport(request: Request, timeout_seconds: float) -> TransportResponse:
    """Send one request with urllib and return HTTP errors as normal responses."""

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return TransportResponse(
                status_code=response.status,
                headers=dict(response.headers.items()),
                body=response.read(),
            )
    except HTTPError as exc:
        try:
            body = exc.read()
        except OSError:
            body = b""
        return TransportResponse(
            status_code=exc.code,
            headers=dict(exc.headers.items()) if exc.headers else {},
            body=body,
        )
    except (URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ApifyTransportError(f"Could not reach the Apify API: {reason}") from exc


class ApifyClient:
    """Authenticated Apify v2 client with mandatory per-run cost caps."""

    def __init__(
        self,
        *,
        token: str | None = None,
        monthly_hard_cap_usd: Decimal | int | float | str | None = None,
        base_url: str = APIFY_API_BASE_URL,
        transport: Transport | None = None,
        sleep: Sleep = time.sleep,
        clock: Clock = time.monotonic,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        resolved_token = token.strip() if token is not None else load_apify_token()
        if not resolved_token:
            raise ApifyConfigurationError("APIFY_TOKEN must not be empty")
        if request_timeout_seconds <= 0 or not math.isfinite(request_timeout_seconds):
            raise ValueError("request_timeout_seconds must be finite and positive")

        self._token = resolved_token
        self._base_url = base_url.rstrip("/")
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self._clock = clock
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._monthly_hard_cap_usd = (
            _positive_decimal(monthly_hard_cap_usd, "monthly_hard_cap_usd")
            if monthly_hard_cap_usd is not None
            else None
        )
        # Prevent two threads sharing this client from both passing the same
        # remaining-budget check before either run is created.
        self._start_lock = threading.Lock()

    def get_current_usage_plan(self) -> UsagePlan:
        """Return current private user/plan data and current account limits/usage."""

        user = self._request_data("GET", "/users/me")
        limits = self._request_data("GET", "/users/me/limits")
        return UsagePlan(user=user, limits=limits)

    def get_account_limits(self) -> dict[str, Any]:
        """Return the exact ``data`` payload from ``/users/me/limits``."""

        return self._request_data("GET", "/users/me/limits")

    def get_actor(self, actor_id: str) -> dict[str, Any]:
        """Return exact Actor metadata from Apify."""

        return self._request_data("GET", f"/actors/{_resource_id(actor_id)}")

    def start_actor(
        self,
        actor_id: str,
        run_input: Mapping[str, Any],
        *,
        max_items: int,
        max_total_charge_usd: Decimal | int | float | str,
    ) -> dict[str, Any]:
        """Start an Actor asynchronously after enforcing all spending caps.

        Both caps are required and are always sent to Apify.  ``maxItems`` caps
        charged dataset items for pay-per-result Actors; ``maxTotalChargeUsd``
        caps the entire run across pricing models.
        """

        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
            raise ValueError("max_items must be a positive integer")
        if not isinstance(run_input, Mapping):
            raise TypeError("run_input must be a JSON object mapping")
        max_charge = _positive_decimal(
            max_total_charge_usd, "max_total_charge_usd"
        )

        with self._start_lock:
            limits = self.get_account_limits()
            self._guard_monthly_cost(limits, max_charge)
            query = urlencode(
                {
                    "maxItems": str(max_items),
                    "maxTotalChargeUsd": _decimal_query_value(max_charge),
                }
            )
            return self._request_data(
                "POST",
                f"/actors/{_resource_id(actor_id)}/runs?{query}",
                json_body=dict(run_input),
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        """Return exact Actor run metadata."""

        return self._request_data("GET", f"/actor-runs/{_resource_id(run_id)}")

    def wait_for_run(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 1800.0,
        poll_interval_seconds: float = 5.0,
        require_success: bool = False,
    ) -> dict[str, Any]:
        """Poll until the run reaches a documented terminal state."""

        if timeout_seconds < 0 or not math.isfinite(timeout_seconds):
            raise ValueError("timeout_seconds must be finite and non-negative")
        if poll_interval_seconds <= 0 or not math.isfinite(poll_interval_seconds):
            raise ValueError("poll_interval_seconds must be finite and positive")

        started_at = self._clock()
        while True:
            run = self.get_run(run_id)
            status = run.get("status")
            if not isinstance(status, str) or not status:
                raise ApifyProtocolError("Actor run response has no string status")
            if status in TERMINAL_RUN_STATUSES:
                if require_success and status != "SUCCEEDED":
                    raise ApifyRunFailedError(run)
                return run

            elapsed = self._clock() - started_at
            if elapsed >= timeout_seconds:
                raise ApifyRunTimeoutError(run_id, timeout_seconds)
            self._sleep(min(poll_interval_seconds, timeout_seconds - elapsed))

    def get_dataset(self, dataset_id: str) -> dict[str, Any]:
        """Return exact dataset metadata."""

        return self._request_data("GET", f"/datasets/{_resource_id(dataset_id)}")

    def get_dataset_items(
        self, dataset_id: str, *, page_size: int = 1000, start_offset: int = 0
    ) -> list[dict[str, Any]]:
        """Download all JSON-object items using stable offset pagination.

        No cleaning, field selection, flattening, unwinding, or other payload
        transformation is requested.  The returned dictionaries are exactly the
        dictionaries decoded from Apify's JSON pages.
        """

        items: list[dict[str, Any]] = []
        for _offset, page, _total in self.iter_dataset_pages(
            dataset_id, page_size=page_size, start_offset=start_offset
        ):
            items.extend(page)
        return items

    def iter_dataset_pages(
        self,
        dataset_id: str,
        *,
        page_size: int = 1000,
        start_offset: int = 0,
    ):
        """Yield exact dataset pages so callers can persist before the next GET."""

        if isinstance(page_size, bool) or not isinstance(page_size, int):
            raise ValueError("page_size must be an integer from 1 to 1000")
        if page_size < 1 or page_size > 1000:
            raise ValueError("page_size must be an integer from 1 to 1000")
        if isinstance(start_offset, bool) or not isinstance(start_offset, int):
            raise ValueError("start_offset must be a non-negative integer")
        if start_offset < 0:
            raise ValueError("start_offset must be a non-negative integer")

        offset = start_offset
        dataset_path = f"/datasets/{_resource_id(dataset_id)}/items"
        while True:
            query = urlencode(
                {
                    "format": "json",
                    "clean": "false",
                    "offset": str(offset),
                    "limit": str(page_size),
                    "desc": "false",
                }
            )
            page, headers = self._request_json("GET", f"{dataset_path}?{query}")
            if not isinstance(page, list):
                raise ApifyProtocolError("Dataset items response must be a JSON array")
            for index, item in enumerate(page):
                if not isinstance(item, dict):
                    raise ApifyProtocolError(
                        f"Dataset item at offset {offset + index} is not a JSON object"
                    )

            count = len(page)
            total = _pagination_total(headers)
            if count == 0:
                if total is not None and offset < total:
                    raise ApifyProtocolError(
                        f"Dataset pagination ended at offset {offset} before total {total}"
                    )
                return
            yield offset, page, total
            offset += count
            if total is not None:
                if offset >= total:
                    return
            elif count < page_size:
                return

    def result_for_run(self, run: Mapping[str, Any]) -> ApifyRunResult:
        """Load default-dataset metadata and exact items for a successful run."""

        if run.get("status") != "SUCCEEDED":
            raise ApifyRunFailedError(run)
        dataset_id = run.get("defaultDatasetId")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ApifyProtocolError(
                "Successful Actor run has no non-empty defaultDatasetId"
            )
        dataset = self.get_dataset(dataset_id)
        items = self.get_dataset_items(dataset_id)
        return ApifyRunResult(run=dict(run), dataset=dataset, items=items)

    def run_actor(
        self,
        actor_id: str,
        run_input: Mapping[str, Any],
        *,
        max_items: int,
        max_total_charge_usd: Decimal | int | float | str,
        timeout_seconds: float = 1800.0,
        poll_interval_seconds: float = 5.0,
    ) -> ApifyRunResult:
        """Start, await, and download one capped Actor run."""

        started = self.start_actor(
            actor_id,
            run_input,
            max_items=max_items,
            max_total_charge_usd=max_total_charge_usd,
        )
        run_id = started.get("id")
        if not isinstance(run_id, str) or not run_id:
            raise ApifyProtocolError("Started Actor run has no non-empty id")
        completed = self.wait_for_run(
            run_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            require_success=True,
        )
        return self.result_for_run(completed)

    def _guard_monthly_cost(
        self, account_data: Mapping[str, Any], requested: Decimal
    ) -> None:
        limits = account_data.get("limits")
        current = account_data.get("current")
        if not isinstance(limits, Mapping) or not isinstance(current, Mapping):
            raise ApifyProtocolError(
                "Account limits response lacks limits/current objects; refusing to run"
            )
        try:
            account_cap = _nonnegative_decimal(
                limits.get("maxMonthlyUsageUsd"),
                "limits.maxMonthlyUsageUsd",
            )
            usage = _nonnegative_decimal(
                current.get("monthlyUsageUsd"),
                "current.monthlyUsageUsd",
            )
        except (TypeError, ValueError) as exc:
            raise ApifyProtocolError(
                "Account limits response has invalid USD values; refusing to run"
            ) from exc

        hard_cap = account_cap
        if self._monthly_hard_cap_usd is not None:
            hard_cap = min(hard_cap, self._monthly_hard_cap_usd)
        remaining = max(Decimal("0"), hard_cap - usage)
        if requested > remaining:
            raise ApifyCostLimitError(
                requested_usd=requested,
                remaining_usd=remaining,
                monthly_hard_cap_usd=hard_cap,
                monthly_usage_usd=usage,
            )

    def _request_data(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload, _headers = self._request_json(method, path, json_body=json_body)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ApifyProtocolError(
                f"Apify {method} response does not contain a data object"
            )
        return payload["data"]

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        if not path.startswith("/"):
            raise ValueError("API path must start with '/'")
        url = f"{self._base_url}{path}"
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "tundi-trip-activity-research/1",
        }
        if json_body is not None:
            try:
                body = json.dumps(
                    json_body,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError("json_body must contain only valid JSON values") from exc
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = Request(url, data=body, headers=headers, method=method)
        try:
            response = self._transport(request, self._request_timeout_seconds)
        except ApifyError:
            raise
        except Exception as exc:
            raise ApifyTransportError("Apify HTTP transport failed") from exc
        if not isinstance(response, TransportResponse):
            raise ApifyTransportError(
                "Injected transport must return a TransportResponse"
            )

        if response.status_code < 200 or response.status_code >= 300:
            error_type, server_message = _parse_error(response.body)
            raise ApifyHttpError(
                status_code=response.status_code,
                method=method,
                url=url,
                error_type=error_type,
                server_message=self._redact(server_message),
                retry_after=_header(response.headers, "Retry-After"),
            )
        try:
            decoded = response.body.decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApifyProtocolError(
                f"Apify {method} response was not valid UTF-8 JSON"
            ) from exc
        return payload, response.headers

    def _redact(self, value: str | None) -> str | None:
        if value is None:
            return None
        return value.replace(self._token, "[REDACTED]")


def _resource_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("resource id must be a non-empty string")
    return quote(value.strip(), safe="~")


def _positive_decimal(value: Any, field: str) -> Decimal:
    result = _decimal(value, field)
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _nonnegative_decimal(value: Any, field: str) -> Decimal:
    result = _decimal(value, field)
    if result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TypeError(f"{field} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite number")
    return result


def _decimal_query_value(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _pagination_total(headers: Mapping[str, str]) -> int | None:
    raw = _header(headers, "X-Apify-Pagination-Total")
    if raw is None:
        return None
    try:
        total = int(raw)
    except ValueError:
        return None
    return total if total >= 0 else None


def _parse_error(body: bytes) -> tuple[str | None, str | None]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, None
    error_type = error.get("type")
    message = error.get("message")
    return (
        error_type if isinstance(error_type, str) else None,
        message if isinstance(message, str) else None,
    )


__all__ = [
    "APIFY_API_BASE_URL",
    "TERMINAL_RUN_STATUSES",
    "ApifyClient",
    "ApifyConfigurationError",
    "ApifyCostLimitError",
    "ApifyError",
    "ApifyHttpError",
    "ApifyProtocolError",
    "ApifyRunFailedError",
    "ApifyRunResult",
    "ApifyRunTimeoutError",
    "ApifyTransportError",
    "TransportResponse",
    "UsagePlan",
    "load_apify_token",
    "urllib_transport",
]
