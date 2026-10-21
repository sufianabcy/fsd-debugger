"""Follow a pipeline's steps through the app's log as it arrives.

A step passes when one of its "passed" regexes matches a log line. It fails
when one of its failure regexes matches a whole event: a log line plus the
lines that belong to it, such as a stack trace with its "Caused by:" chain or
the APPLICATION FAILED TO START box.

Startup is one run through its pipeline. Requests are many: each request's
lines share a thread name, so RequestTracker keeps one trace per thread, from
the request's first line to its last.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from importlib import resources

import yaml

PIPELINES = resources.files("fsd") / "pipelines"

# A Spring Boot log line starts with a timestamp, a Maven line with [INFO] and
# so on. Any other line continues the event before it.
EVENT_START = re.compile(r"^(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\[(?:INFO|WARNING|ERROR|DEBUG)\](?: |$))")
ANSI_CODE = re.compile(r"\x1b\[[0-9;]*m")
# The thread is the last [...] after "---" that is not followed by another [:
# --- [demo-app] [nio-8080-exec-1] o.s.web.servlet.DispatcherServlet : ...
THREAD = re.compile(r"---\s+(?:\[[^\]]*\]\s*)*?\[\s*(?P<thread>[^\]]*?)\s*\]\s+(?!\[)")
# A stack frame: at com.demo.service.UserService.findAll(UserService.java:20)
FRAME = re.compile(
    r"^\s+at (?:[\w.$-]*/)*(?P<class_name>[\w.$]+)\.(?P<method>[\w$<>-]+)\((?P<file>[^:)]*)(?::(?P<line>\d+))?\)",
    re.MULTILINE,
)


class Status(Enum):
    WAITING = "waiting"
    PASSED = "passed"
    FAILED = "failed"


def found_details(match: re.Match) -> dict[str, str]:
    return {name: value for name, value in match.groupdict().items() if value is not None}


@dataclass
class FailureRule:
    id: str
    title: str
    patterns: list[re.Pattern]
    message: str = ""
    checklist: str = ""  # the checks that look for the cause, by name
    generic: bool = False  # any exception, say: a specific rule from any step wins over it

    def match(self, event: str) -> dict[str, str] | None:
        """Return the details named in the first pattern that matches `event`, or None."""
        for pattern in self.patterns:
            found = pattern.search(event)
            if found:
                return found_details(found)
        return None


@dataclass
class Step:
    id: str
    name: str
    passed: list[re.Pattern] = field(default_factory=list)
    failures: list[FailureRule] = field(default_factory=list)
    note: str = ""

    def match_passed(self, line: str) -> dict[str, str] | None:
        for pattern in self.passed:
            found = pattern.search(line)
            if found:
                return found_details(found)
        return None


@dataclass
class TraceRules:
    """Where one request's lines begin and end, for a pipeline that follows requests."""

    start: re.Pattern
    end: list[re.Pattern]
    error_dispatch: re.Pattern | None = None  # lines from here on belong to the error page, not to a step


@dataclass
class Pipeline:
    title: str
    steps: list[Step]
    fallback: FailureRule | None = None
    trace: TraceRules | None = None

    def rule(self, rule_id: str) -> tuple[Step, FailureRule]:
        """The failure rule with `rule_id`, and the step it belongs to."""
        return next((step, rule) for step in self.steps for rule in step.failures if rule.id == rule_id)


def load_rule(data: dict) -> FailureRule:
    return FailureRule(
        id=data["id"],
        title=data["title"],
        patterns=[re.compile(p, re.MULTILINE) for p in data["patterns"]],
        message=data.get("message", ""),
        checklist=data.get("checklist", ""),
        generic=data.get("generic", False),
    )


def load_step(data: dict) -> Step:
    return Step(
        id=data["id"],
        name=data["name"],
        passed=[re.compile(p) for p in data.get("passed", [])],
        failures=[load_rule(rule) for rule in data.get("failures", [])],
        note=data.get("note", ""),
    )


def load_trace_rules(data: dict) -> TraceRules:
    error_dispatch = data.get("error_dispatch")
    return TraceRules(
        start=re.compile(data["start"]),
        end=[re.compile(p) for p in data["end"]],
        error_dispatch=re.compile(error_dispatch) if error_dispatch else None,
    )


def load_pipeline(name: str) -> Pipeline:
    """Read fsd/pipelines/<name>.yaml."""
    data = yaml.safe_load((PIPELINES / f"{name}.yaml").read_text(encoding="utf-8"))
    fallback = data.get("fallback")
    trace = data.get("trace")
    return Pipeline(
        title=data["title"],
        steps=[load_step(step) for step in data["steps"]],
        fallback=load_rule(fallback) if fallback else None,
        trace=load_trace_rules(trace) if trace else None,
    )


def clean(line: str) -> str:
    return ANSI_CODE.sub("", line.rstrip("\r\n"))


def starts_event(line: str) -> bool:
    return EVENT_START.match(line) is not None


def thread_of(line: str) -> str | None:
    found = THREAD.search(line)
    return found["thread"] if found else None


class EventGrouper:
    """Join each log line with the lines that continue it into one event."""

    def __init__(self):
        self.lines: list[str] = []

    def add(self, line: str) -> str | None:
        """Add a line. If it starts a new event, return the event it completes."""
        finished = self.flush() if self.lines and starts_event(line) else None
        self.lines.append(line)
        return finished

    def flush(self) -> str | None:
        """Return the event in progress, or None if there is none."""
        if not self.lines:
            return None
        event = "\n".join(self.lines)
        self.lines = []
        return event


@dataclass
class Failure:
    step: Step
    rule: FailureRule
    details: dict[str, str]
    event: str
    known_cause: bool = True


class PipelineRun:
    """One pass through a pipeline: which steps passed, which failed, and why."""

    def __init__(self, pipeline: Pipeline):
        self.pipeline = pipeline
        self.status = {step.id: Status.WAITING for step in pipeline.steps}
        self.details: dict[str, dict[str, str]] = {step.id: {} for step in pipeline.steps}
        self.failure: Failure | None = None  # the cause: the earliest step that failed
        self.failures: dict[str, Failure] = {}  # every failed step: the cause, and later ones like a 500 status
        self.position = {step.id: index for index, step in enumerate(pipeline.steps)}

    @property
    def all_passed(self) -> bool:
        return all(status is Status.PASSED for status in self.status.values())

    @property
    def has_progress(self) -> bool:
        return any(status is not Status.WAITING for status in self.status.values())

    def current_step(self) -> Step:
        """The first step that has not passed yet."""
        return next(s for s in self.pipeline.steps if self.status[s.id] is not Status.PASSED)

    def pass_steps_before(self, step: Step, including: bool = False) -> None:
        end = self.position[step.id] + (1 if including else 0)
        for earlier in self.pipeline.steps[:end]:
            if self.status[earlier.id] is Status.WAITING:
                self.status[earlier.id] = Status.PASSED

    def check_passed(self, line: str) -> None:
        for step in self.pipeline.steps:
            if self.status[step.id] is not Status.WAITING:
                continue
            found = step.match_passed(line)
            if found is not None:
                self.details[step.id].update(found)
                self.pass_steps_before(step, including=True)

    def check_event(self, event: str) -> None:
        if self.all_passed:
            return
        failure = self.match_known_cause(event) or self.match_fallback(event)
        if failure is not None:
            self.record(failure)

    def match_known_cause(self, event: str) -> Failure | None:
        """The earliest step with a failure pattern that matches `event`; a generic rule only if no specific one does."""
        rules = [(step, rule) for step in self.pipeline.steps for rule in step.failures]
        for step, rule in sorted(rules, key=lambda pair: pair[1].generic):
            details = rule.match(event)
            if details is not None:
                return Failure(step, rule, details, event)
        return None

    def match_fallback(self, event: str) -> Failure | None:
        rule = self.pipeline.fallback
        if rule is None or rule.match(event) is None:
            return None
        step = self.failure.step if self.failure else self.current_step()
        return Failure(step, rule, {}, event, known_cause=False)

    def replaces(self, new: Failure, old: Failure) -> bool:
        """A known cause beats a fallback, a specific rule a generic one, and an earlier step a later one."""
        if new.known_cause != old.known_cause:
            return new.known_cause
        if new.rule.generic != old.rule.generic:
            return old.rule.generic
        return self.position[new.step.id] < self.position[old.step.id]

    def record(self, new: Failure) -> None:
        cause = self.failure
        if cause is None or self.replaces(new, cause):
            self.record_cause(new)
        elif new.step.id in self.failures:
            for name, value in new.details.items():
                self.failures[new.step.id].details.setdefault(name, value)
        elif new.known_cause and self.position[new.step.id] > self.position[cause.step.id]:
            # A later step failed too, as a result: the response status after an exception.
            self.failures[new.step.id] = new
            self.status[new.step.id] = Status.FAILED

    def record_cause(self, new: Failure) -> None:
        old = self.failure
        if old is not None and (not old.known_cause or old.rule.generic):
            # A guess, or a generic exception: the new cause takes its place.
            del self.failures[old.step.id]
            self.status[old.step.id] = Status.WAITING
        self.failure = new
        self.failures[new.step.id] = new
        self.pass_steps_before(new.step)
        self.status[new.step.id] = Status.FAILED


class Tracker(PipelineRun):
    """Run a pipeline once over the app's log, line by line: startup."""

    def __init__(self, pipeline: Pipeline):
        super().__init__(pipeline)
        self.events = EventGrouper()

    def feed(self, line: str) -> None:
        line = clean(line)
        event = self.events.add(line)
        if event is not None:
            self.check_event(event)
        if self.failure is None:
            self.check_passed(line)

    def finish(self) -> None:
        """Check the last event, once the app's output has ended."""
        event = self.events.flush()
        if event is not None:
            self.check_event(event)


@dataclass
class Frame:
    class_name: str  # com.demo.service.UserService
    method: str
    file: str  # UserService.java
    line: int | None


@dataclass
class CodeFrame:
    role: str  # controller, service, repository, component or class
    frame: Frame


@dataclass
class Trace:
    """One request, from its first log line to its last."""

    method: str
    path: str
    thread: str
    run: PipelineRun
    status: int | None = None  # None if the log never showed how it ended
    lines: list[str] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)  # every stack frame logged during it, innermost first
    finished_at: float = 0.0  # time.time() when fsd saw it end
    in_error_dispatch: bool = False

    @property
    def succeeded(self) -> bool:
        """Answered below 400 with no failed step. A 200 can still fail: once the response is
        committed, an error while writing it cannot change the status."""
        return self.status is not None and self.status < 400 and self.run.failure is None


def frames_in(event: str) -> list[Frame]:
    return [
        Frame(found["class_name"], found["method"], found["file"], int(found["line"]) if found["line"] else None)
        for found in FRAME.finditer(event)
    ]


def code_path(frames: list[Frame], role_of: Callable[[str], str | None]) -> list[CodeFrame]:
    """The frames in the app's own classes, outermost call first, each labeled with its role."""
    seen, path = set(), []
    for frame in frames:
        role = role_of(frame.class_name)
        key = (frame.class_name, frame.method, frame.line)
        if role and key not in seen:
            seen.add(key)
            path.append(CodeFrame(role, frame))
    return path[::-1]  # a stack trace lists the innermost call first


class RequestTracker:
    """Follow each request through a pipeline: one trace per request, found by its thread name."""

    def __init__(self, pipeline: Pipeline, on_finish: Callable[[Trace], None]):
        self.pipeline = pipeline
        self.rules = pipeline.trace
        self.on_finish = on_finish
        self.events = EventGrouper()
        self.open: dict[str, Trace] = {}
        self.thread: str | None = None  # the thread of the event the current line belongs to

    def feed(self, line: str) -> None:
        line = clean(line)
        event = self.events.add(line)
        if event is not None:
            self.check_event(event)
        if starts_event(line):
            self.thread = thread_of(line)
        if trace := self.trace_for(line):
            trace.lines.append(line)
            self.check_line(trace, line)

    def finish(self) -> None:
        """Check the last event and close every request still open, once the app's output has ended."""
        event = self.events.flush()
        if event is not None:
            self.check_event(event)
        for trace in list(self.open.values()):
            self.close(trace)

    def trace_for(self, line: str) -> Trace | None:
        """The open trace `line` belongs to; a request's first line opens a new one."""
        if self.thread is None:
            return None
        if starts_event(line) and (start := self.rules.start.search(line)):
            if unfinished := self.open.get(self.thread):
                self.close(unfinished)  # it never logged its end
            run = PipelineRun(self.pipeline)
            self.open[self.thread] = Trace(start["method"], start["path"], self.thread, run)
        return self.open.get(self.thread)

    def check_line(self, trace: Trace, line: str) -> None:
        if self.rules.error_dispatch and self.rules.error_dispatch.search(line):
            trace.in_error_dispatch = True
        elif not trace.in_error_dispatch and trace.run.failure is None:
            trace.run.check_passed(line)
        if end := next((found for pattern in self.rules.end if (found := pattern.search(line))), None):
            trace.status = int(end["status"])
            trace.run.check_event(line)  # the last line can fail the last step: a 404 or a 500
            self.close(trace)

    def check_event(self, event: str) -> None:
        trace = self.open.get(thread_of(event.split("\n", 1)[0]))
        if trace is None:
            return
        trace.frames += frames_in(event)
        trace.run.check_event(event)

    def close(self, trace: Trace) -> None:
        del self.open[trace.thread]
        trace.finished_at = time.time()
        self.on_finish(trace)


def trace_from_lines(pipeline: Pipeline, lines: list[str]) -> Trace | None:
    """Rebuild a trace from its log lines, as fsd try does from the session file."""
    found: list[Trace] = []
    tracker = RequestTracker(pipeline, found.append)
    for line in lines:
        tracker.feed(line)
    tracker.finish()
    return found[0] if found else None
