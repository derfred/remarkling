import json
import logging

from inkbridge.logs import JsonFormatter


def _record(level=logging.INFO, msg="poll finished", args=None, name="inkbridge.poller", exc_info=None, **extra):
    r = logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)
    r.__dict__.update(extra)
    return r


def test_one_json_line_with_extra_fields():
    out = json.loads(JsonFormatter().format(_record(trigger="wake", pulled=2, duration_s=0.4)))
    assert out["ts"].endswith("Z") and "T" in out["ts"]
    assert {k: v for k, v in out.items() if k != "ts"} == {
        "level": "info", "logger": "inkbridge.poller", "msg": "poll finished",
        "trigger": "wake", "pulled": 2, "duration_s": 0.4,
    }


def test_levels_are_lowercase_and_warning_is_warn():
    levels = [json.loads(JsonFormatter().format(_record(level)))["level"]
              for level in (logging.DEBUG, logging.WARNING, logging.ERROR, logging.CRITICAL)]
    assert levels == ["debug", "warn", "error", "error"]


def test_exception_stays_on_one_line_with_the_stack_in_a_field():
    try:
        raise ValueError("bad page")
    except ValueError:
        import sys
        line = JsonFormatter().format(_record(logging.ERROR, "pull failed", exc_info=sys.exc_info()))
    assert "\n" not in line
    out = json.loads(line)
    assert out["error"] == "bad page" and out["error_type"] == "ValueError"
    assert out["stack"].startswith("Traceback") and "\n" in out["stack"]


def test_uvicorn_access_log_becomes_fields():
    r = _record(msg='%s - "%s %s HTTP/%s" %d', args=("10.0.0.1:5000", "GET", "/health", "1.1", 200),
                name="uvicorn.access")
    out = json.loads(JsonFormatter().format(r))
    assert out["msg"] == "http request"
    assert (out["method"], out["path"], out["status"], out["client"]) == ("GET", "/health", 200, "10.0.0.1:5000")


def test_values_that_are_not_json_are_stringified():
    out = json.loads(JsonFormatter().format(_record(path=__import__("pathlib").Path("/data/x"))))
    assert out["path"] == "/data/x"
