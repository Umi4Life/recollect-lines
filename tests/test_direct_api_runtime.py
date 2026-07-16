import json
import importlib.util
import io
import os
import sys
import tempfile
import time
import unittest
import warnings
from contextlib import contextmanager
from pathlib import Path

from recollect_lines.direct_api_runtime import OpenAiCompatibleDirectRuntime
from recollect_lines.models import TaskRecord, TaskRequest, TaskState
from recollect_lines.providers import (
    MissingCredentialReference,
    ProviderConfigError,
    redact_provider_error,
    resolve_api_key,
    validate_providers_document,
)
from recollect_lines.service import Broker

TLS_CERT = Path(__file__).parent / "fixtures" / "tls" / "self_signed_cert.pem"
TLS_KEY = Path(__file__).parent / "fixtures" / "tls" / "self_signed_key.pem"
FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "fake_openai_server.py"
_spec = importlib.util.spec_from_file_location("fake_openai_server", FIXTURE_SERVER)
assert _spec and _spec.loader
_fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fake)
FakeOpenAiServer = _fake.FakeOpenAiServer
provider_document = _fake.provider_document


@contextmanager
def _capture_stderr():
    prior = sys.stderr
    buf = io.StringIO()
    sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stderr = prior


class ProviderConfigTests(unittest.TestCase):
    def test_accepts_plural_named_providers(self):
        configs = validate_providers_document({
            "providers": {
                "alpha": {
                    "kind": "openai-compatible",
                    "base_url": "https://api.example.com/v1",
                    "api_key_env": "ALPHA_KEY",
                    "default_model": "alpha-model",
                },
                "beta": {
                    "kind": "openai-compatible",
                    "base_url": "https://other.example.com/v1",
                    "api_key_env": "BETA_KEY",
                    "default_model": "beta-model",
                },
            }
        })
        self.assertEqual(set(configs), {"alpha", "beta"})

    def test_rejects_malformed_provider_names(self):
        with self.assertRaises(ProviderConfigError):
            validate_providers_document({
                "providers": {
                    "Bad-Name": {
                        "kind": "openai-compatible",
                        "base_url": "https://a.example/v1",
                        "api_key_env": "A",
                        "default_model": "m",
                    }
                }
            })

    def test_rejects_remote_http_without_explicit_opt_in(self):
        with self.assertRaises(ProviderConfigError):
            validate_providers_document({
                "providers": {
                    "bad": {
                        "kind": "openai-compatible",
                        "base_url": "http://203.0.113.1/v1",
                        "api_key_env": "K",
                        "default_model": "m",
                        "allow_insecure_http": True,
                    }
                }
            })

    def test_requires_allow_insecure_http_for_loopback_http(self):
        with self.assertRaises(ProviderConfigError):
            validate_providers_document({
                "providers": {
                    "local": {
                        "kind": "openai-compatible",
                        "base_url": "http://127.0.0.1:8000/v1",
                        "api_key_env": "K",
                        "default_model": "m",
                    }
                }
            })

    def test_resolve_api_key_fails_closed(self):
        config = validate_providers_document({
            "providers": {
                "x": {
                    "kind": "openai-compatible",
                    "base_url": "https://api.example.com/v1",
                    "api_key_env": "MISSING_ENV_VAR",
                    "default_model": "m",
                }
            }
        })["x"]
        with self.assertRaises(MissingCredentialReference):
            resolve_api_key(config, {})

    def test_redact_provider_error_strips_secret(self):
        secret = "sk-testsecret1234567890"
        message = f"boom bearer {secret} and api_key={secret}"
        redacted = redact_provider_error(message, secret)
        self.assertNotIn(secret, redacted)
        self.assertIn("***REDACTED***", redacted)


class FakeOpenAiServerLifecycleTests(unittest.TestCase):
    def test_stop_closes_listening_socket_without_resource_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            server = FakeOpenAiServer()
            server.start()
            server.stop()
        socket_leaks = [
            warning
            for warning in caught
            if issubclass(warning.category, ResourceWarning)
            and "unclosed" in str(warning.message).lower()
            and "socket" in str(warning.message).lower()
        ]
        self.assertEqual(socket_leaks, [])


class DirectApiBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.server = FakeOpenAiServer()
        self.server.start()
        self.env = {"TEST_OPENAI_API_KEY": "sk-fake-test-key-not-real"}
        config_path = Path(self.tempdir.name) / "providers.json"
        config_path.write_text(json.dumps(provider_document(self.server.base_url)) + "\n")
        self.config_path = config_path
        self.broker = Broker(self.home, providers_config=config_path, environ=self.env)

    def tearDown(self):
        self.broker.close()
        self.server.stop()
        self.tempdir.cleanup()

    def create(self, task: str, provider: str = "local", **kwargs):
        request = TaskRequest(task, "/repo", profile="openai_compatible", provider=provider, **kwargs)
        return self.broker.create(request)

    def test_success_collects_chat_completion_summary(self):
        record = self.create("What is 2+2?")
        self.broker.start(record.id)
        time.sleep(0.3)
        collected = self.broker.collect(record.id)
        self.assertEqual(collected.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertIn("answer for:", result["summary"])
        self.assertEqual(result["runtime"]["adapter"], "openai_compatible")
        self.assertIn("limitations", result["runtime"])

    def test_missing_secret_reference_fails_without_leaking(self):
        broker = Broker(self.home / "missing", providers_config=self.config_path, environ={})
        try:
            record = broker.create(TaskRequest("hi", "/repo", profile="openai_compatible", provider="local"))
            started = broker.start(record.id)
            self.assertEqual(started.state, TaskState.FAILED)
            events = broker.store.events(record.id)
            combined = json.dumps(events)
            self.assertNotIn("sk-fake", combined)
        finally:
            broker.close()

    def test_malformed_response_is_failed(self):
        record = self.create("MALFORMED_BODY please")
        self.broker.start(record.id)
        time.sleep(0.3)
        collected = self.broker.collect(record.id)
        self.assertEqual(collected.state, TaskState.FAILED)

    def test_rate_limit_is_normalized(self):
        record = self.create("RATE_LIMIT scenario")
        self.broker.start(record.id)
        time.sleep(0.3)
        collected = self.broker.collect(record.id)
        self.assertEqual(collected.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["error_category"], "rate_limit_or_quota_error")

    def test_timeout_transitions_to_timed_out(self):
        doc = provider_document(self.server.base_url, request_timeout_seconds=1)
        path = Path(self.tempdir.name) / "slow-providers.json"
        path.write_text(json.dumps(doc) + "\n")
        broker = Broker(self.home / "slow", providers_config=path, environ=self.env)
        try:
            record = broker.create(TaskRequest("SLOW request", "/repo", profile="openai_compatible", provider="local"))
            broker.start(record.id)
            time.sleep(1.5)
            timed = broker.timeout(record.id, reason="test timeout")
            self.assertIn(timed.state, {TaskState.TIMED_OUT, TaskState.RECOVERY_REQUIRED, TaskState.FAILED})
        finally:
            broker.close()

    def test_provider_deadline_exhausts_naturally_without_an_external_timeout_signal(self):
        # Distinct from test_timeout_transitions_to_timed_out above: that test
        # exercises the broker's *external* timeout signal (an operator or
        # liveness component calling broker.timeout()). This one exercises the
        # runtime's own internal per-request deadline
        # (direct_api_runtime.py _post_chat_completions' `while
        # time.monotonic() < deadline` loop) expiring entirely on its own,
        # with no external intervention — provider deadline exhaustion from
        # the Wave 0 dogfood findings.
        doc = provider_document(self.server.base_url, request_timeout_seconds=1)
        path = Path(self.tempdir.name) / "deadline-exhaustion-providers.json"
        path.write_text(json.dumps(doc) + "\n")
        broker = Broker(self.home / "deadline-exhaustion", providers_config=path, environ=self.env)
        try:
            record = broker.create(TaskRequest("SLOW request", "/repo", profile="openai_compatible", provider="local"))
            broker.start(record.id)
            time.sleep(1.6)
            collected = broker.collect(record.id)
            self.assertEqual(collected.state, TaskState.FAILED)
            result = json.loads(
                (self.home / "deadline-exhaustion" / "artifacts" / record.id / "result.json").read_text()
            )
            self.assertEqual(result["runtime"]["error_category"], "runtime_error")
            self.assertIn("timed out after 1s", result["runtime"]["error_message"])
        finally:
            broker.close()

    def test_response_past_five_second_attempt_cap_but_within_provider_deadline_currently_still_fails(self):
        # Wave 0 dogfood finding: OpenAiCompatibleDirectRuntime hardcodes
        # every retry attempt's urlopen timeout to at most 5s
        # (direct_api_runtime.py _post_chat_completions), regardless of how
        # much of the overall provider deadline remains. A provider that
        # legitimately takes just over 5s to respond — but comfortably fits
        # a larger deadline — gets every attempt aborted at the 5s mark and
        # retried, so it never actually receives the response that was on
        # its way. This pins that current (pre-fix) behavior as a
        # regression baseline, not a target: a fix would let a single
        # attempt use the full remaining deadline budget instead of a flat
        # 5s ceiling.
        doc = provider_document(self.server.base_url, request_timeout_seconds=6)
        path = Path(self.tempdir.name) / "slow-past-cap-providers.json"
        path.write_text(json.dumps(doc) + "\n")
        broker = Broker(self.home / "slow-past-cap", providers_config=path, environ=self.env)
        try:
            record = broker.create(TaskRequest(
                "SLOW_PAST_ATTEMPT_CAP request", "/repo", profile="openai_compatible", provider="local",
            ))
            broker.start(record.id)
            time.sleep(7.2)
            collected = broker.collect(record.id)
            self.assertEqual(collected.state, TaskState.FAILED)
            result = json.loads(
                (self.home / "slow-past-cap" / "artifacts" / record.id / "result.json").read_text()
            )
            self.assertEqual(result["runtime"]["error_category"], "runtime_error")
            self.assertIn("timed out after 6s", result["runtime"]["error_message"])
        finally:
            broker.close()

    def test_cancel_running_direct_api_request(self):
        doc = provider_document(self.server.base_url, request_timeout_seconds=30)
        path = Path(self.tempdir.name) / "cancel-providers.json"
        path.write_text(json.dumps(doc) + "\n")
        broker = Broker(self.home / "cancel", providers_config=path, environ=self.env)
        try:
            record = broker.create(TaskRequest("SLOW cancel me", "/repo", profile="openai_compatible", provider="local"))
            broker.start(record.id)
            time.sleep(0.05)
            cancelled = broker.cancel(record.id, "stop")
            self.assertIn(cancelled.state, {TaskState.CANCELLED, TaskState.FAILED})
        finally:
            broker.close()

    def test_cancel_slow_request_does_not_emit_server_traceback(self):
        doc = provider_document(self.server.base_url, request_timeout_seconds=30)
        path = Path(self.tempdir.name) / "cancel-stderr-providers.json"
        path.write_text(json.dumps(doc) + "\n")
        broker = Broker(self.home / "cancel-stderr", providers_config=path, environ=self.env)
        try:
            with _capture_stderr() as captured:
                record = broker.create(TaskRequest("SLOW cancel me", "/repo", profile="openai_compatible", provider="local"))
                broker.start(record.id)
                time.sleep(0.05)
                broker.cancel(record.id, "stop")
                time.sleep(3.2)
            noise = captured.getvalue()
            self.assertNotIn("Traceback", noise)
            self.assertNotIn("BrokenPipeError", noise)
        finally:
            broker.close()

    def test_redacts_secret_material_from_summary(self):
        record = self.create("SECRET_LEAK check")
        self.broker.start(record.id)
        time.sleep(0.3)
        collected = self.broker.collect(record.id)
        result = json.loads((self.home / "artifacts" / collected.id / "result.json").read_text())
        self.assertNotIn("sk-testsecret", result["summary"])

    def test_rejects_isolated_worktree_for_direct_api(self):
        with self.assertRaisesRegex(ValueError, "does not permit mode"):
            self.create("nope", execution_mode="isolated_worktree")

    def test_requires_named_provider_on_create(self):
        with self.assertRaisesRegex(ValueError, "requires a named provider"):
            self.broker.create(TaskRequest("hi", "/repo", profile="openai_compatible"))

    def test_multiple_named_providers(self):
        doc = {
            "providers": {
                "first": {
                    "kind": "openai-compatible",
                    "base_url": self.server.base_url,
                    "api_key_env": "TEST_OPENAI_API_KEY",
                    "default_model": "fake-model",
                    "request_timeout_seconds": 2,
                    "allow_insecure_http": True,
                },
                "second": {
                    "kind": "openai-compatible",
                    "base_url": self.server.base_url,
                    "api_key_env": "TEST_OPENAI_API_KEY",
                    "default_model": "other-model",
                    "request_timeout_seconds": 2,
                    "allow_insecure_http": True,
                },
            }
        }
        path = Path(self.tempdir.name) / "multi.json"
        path.write_text(json.dumps(doc) + "\n")
        broker = Broker(self.home / "multi", providers_config=path, environ=self.env)
        try:
            r1 = broker.create(TaskRequest("via first", "/repo", profile="openai_compatible", provider="first"))
            r2 = broker.create(TaskRequest("via second", "/repo", profile="openai_compatible", provider="second"))
            broker.start(r1.id)
            broker.start(r2.id)
            time.sleep(0.4)
            self.assertEqual(broker.collect(r1.id).state, TaskState.SUCCEEDED)
            self.assertEqual(broker.collect(r2.id).state, TaskState.SUCCEEDED)
        finally:
            broker.close()

    def test_restart_reconcile_fails_honestly(self):
        record = self.create("in flight")
        self.broker.start(record.id)
        self.broker.close()
        broker2 = Broker(self.home, providers_config=self.config_path, environ=self.env)
        try:
            reconciled = broker2.reconcile(record.id)
            self.assertEqual(reconciled.state, TaskState.FAILED)
        finally:
            broker2.close()


class DirectApiTlsCertificateVerificationTests(unittest.TestCase):
    """A provider whose TLS certificate the client cannot verify (Wave 0
    dogfood finding, 2026-07-16). The runtime's default SSL context enforces
    verification (OpenAiCompatibleDirectRuntime._build_ssl_context uses
    ssl.create_default_context()), so a self-signed cert that never chains to
    a trusted root must fail the connection. Reproduced fully locally with a
    fixed, committed self-signed cert (tests/fixtures/tls/) — no LINE
    endpoint, real credential, or machine CA path involved.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.server = FakeOpenAiServer(certfile=str(TLS_CERT), keyfile=str(TLS_KEY))
        self.server.start()
        self.env = {"TEST_OPENAI_API_KEY": "sk-fake-test-key-not-real"}
        doc = provider_document(self.server.base_url, request_timeout_seconds=1, allow_insecure_http=False)
        config_path = Path(self.tempdir.name) / "providers.json"
        config_path.write_text(json.dumps(doc) + "\n")
        self.broker = Broker(self.home, providers_config=config_path, environ=self.env)

    def tearDown(self):
        self.broker.close()
        self.server.stop()
        self.tempdir.cleanup()

    def test_untrusted_cert_currently_surfaces_as_a_generic_timeout_not_a_tls_error(self):
        # Pre-fix baseline: every attempt's certificate-verify failure raises
        # urllib.error.URLError, which the retry loop treats identically to a
        # transient network blip — it retries until the provider deadline
        # elapses, then raises a generic TimeoutError whose message never
        # mentions the real cause. Asserting a TLS-specific error_category
        # here would assert a product promise the runtime doesn't keep yet;
        # this pins today's actual, observable failure path instead.
        record = self.broker.create(TaskRequest("hello", "/repo", profile="openai_compatible", provider="local"))
        self.broker.start(record.id)
        time.sleep(1.6)
        collected = self.broker.collect(record.id)
        self.assertEqual(collected.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["error_category"], "runtime_error")
        self.assertIn("timed out", result["runtime"]["error_message"])
        self.assertNotIn("sk-fake", result["runtime"]["error_message"])


class DirectApiRuntimeExecutionModePolicyTests(unittest.TestCase):
    """The broker's profile-policy gate (openai_compatible's allowed_modes =
    {"read_only"}, see models.DEFAULT_PROFILES) already rejects
    isolated_worktree before any runtime is touched — see
    DirectApiBrokerTests.test_rejects_isolated_worktree_for_direct_api. This
    pins the runtime's own defense-in-depth check (direct_api_runtime.py
    start()) directly, so a future policy misconfiguration can never
    silently reach a runtime with no honest worktree/tool support.
    """

    def test_start_rejects_isolated_worktree_even_if_the_policy_gate_is_bypassed(self):
        providers = validate_providers_document(
            provider_document("https://api.example.com/v1", allow_insecure_http=False)
        )
        runtime = OpenAiCompatibleDirectRuntime(providers, environ={"TEST_OPENAI_API_KEY": "sk-fake-test-key"})
        request = TaskRequest(
            "task", "/repo", execution_mode="isolated_worktree", profile="openai_compatible", provider="local",
        )
        record = TaskRecord.new(request)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ProviderConfigError, "read_only"):
                runtime.start(record, Path(tmp))


if __name__ == "__main__":
    unittest.main()
