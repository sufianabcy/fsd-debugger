# fsd: step-by-step flow tracer for Spring Boot

fsd is a Python CLI that runs a Spring Boot app, follows its startup and
request flow step by step from the logs, and when a step fails runs a fixed
checklist of causes against the source code and prints the fix.
Full design: docs/design.md. Read it before planning any change.

## Hard rules
- No AI, LLM or network API calls anywhere in the tool. All diagnosis is
  rule-based: YAML pipelines + Python check functions.
- Never change the user's project files. Only read them.
- Pass the app's own output through unchanged; fsd output is added, not mixed in.

## Stack
- Python 3.11+, Typer (CLI), Rich (panel), PyYAML, pytest.
- Package in fsd/, pipelines in fsd/pipelines/*.yaml
  (package data, loaded with importlib.resources), tests in tests/,
  real sample logs in tests/logs/*.log.
- samples/demo-app is a Spring Boot app used to produce real logs. Break it
  on purpose to capture a log, then restore it.
- Target Spring Boot 4.x but keep patterns tolerant of 3.x. The logs in
  tests/logs are the source of truth for log wording, not the lines quoted
  in docs/design.md.
- Never hard-code the main class name, Java version or port in a pattern.

## Commands
- Install: pip install -e ".[dev]"
- Test: pytest -q
- Run: fsd run -- ./mvnw spring-boot:run   (from samples/demo-app)

## Working rules
- Every new step, failure pattern or check gets a test using a real log
  from tests/logs/.
- Keep functions small and named for what they check, e.g.
  has_stereotype_annotation(class_name).
- Run pytest before saying a task is done.