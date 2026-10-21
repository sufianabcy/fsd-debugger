Full-Stack Debugger: existing tools and approach
Oct 20, 2026 · @Sufian
Short answer
Pieces of this exist, but nothing does exactly what you described: a small, offline tool that sits next to your running app, reads its logs, and says "you probably forgot @Service on UserService" or "you called /display but the only close mapping is /disply/". Spring Boot itself already detects many of these failures, and IntelliJ catches some before you run, but their messages are written for experienced developers. AI assistants explain well but need you to copy and paste, and they guess.
Recommendation: build a step-by-step flow tracer, with no AI. You start your app through it (fsd run -- ./mvnw spring-boot:run). It knows the steps a Spring Boot app goes through, at startup and for every request, and ticks each one off as the logs show it passing. When something breaks, it shows which step failed, runs a fixed checklist of known causes for that step against your code, and prints the cause it found and the fix. A second command, fsd doctor, runs the same checks on your project before you even start it.
What already exists
Each tool below covers part of the problem. The gap they share is that none of them reads your running output, connects it back to your source code, and explains it in beginner terms without a copy and paste.
Tool
What it does for your two examples
Where it falls short
Spring Boot FailureAnalyzers (built in)
On a missing @Service, prints the "APPLICATION FAILED TO START" box: "Parameter 0 of constructor in UserController required a bean of type 'UserService' that could not be found" plus "Action: Consider defining a bean of type…"
Only startup failures. The "Action" line is generic and never says "add @Service" or "your class is outside the scanned package". Does nothing for 404s.
Spring Boot Actuator /actuator/mappings
Lists every URL your app really exposes, so you can see /disply/ is there and /display is not
You must add the dependency, open the endpoint and compare by eye. No "did you mean".
Spring's own 404 logging
Spring Boot 3.2+ logs NoResourceFoundException: No static resource display at WARN; older versions show the Whitelabel page and log "No mapping for GET /display" only at DEBUG
The message talks about static resources, which confuses beginners. It never suggests the nearest real mapping.
IntelliJ IDEA Ultimate, Spring support
Flags "Could not autowire. No beans of type 'UserService' found" in the editor before you run. Endpoints tool window lists your mappings.
Paid edition. Catches the bean case, not the typo in a URL you call from the browser or frontend.
Spring Tools (VS Code, Eclipse)
Free. Live hovers show beans and request mappings of a running app; some validations.
Lighter than IntelliJ, no explanations of log errors.
SonarLint / SonarQube, Checkstyle, PMD
Static rules on code quality and bugs
Not aware of Spring wiring or runtime URLs; won't catch either example.
Log viewers: lnav, ELK, Grafana Loki, Datadog, Sentry
Search, filter and group logs; Sentry groups exceptions
Built for production operations, not for teaching. They show the error, they don't explain it.
AI in the IDE: JetBrains AI Assistant ("Explain with AI" on console stack traces), GitHub Copilot Chat, Cursor
Explains a pasted or selected stack trace well
Needs a click or paste each time, needs a subscription and network, may guess, and doesn't know which URL you actually requested.
AI terminals: Warp
Can explain the last command's error output
Same as above, plus tied to one terminal app.
thefuck (CLI)
Not for Spring, but the closest design match: watches the last command's failure and suggests a fix from a library of small rules
Shell commands only. Worth copying its rule-plugin design.
OpenTelemetry Java agent + Jaeger or Zipkin
Records each request as a trace (controller, JDBC calls, timings) and shows it as a waterfall, with the failing span marked
Shows where a request failed, not why or how to fix it. Setup (agent jar, collector, UI) is heavy for a learner, and it doesn't cover startup failures.
The two lessons: Spring already gives you the raw facts (bean type, constructor, requested path, real mappings), and rule libraries like thefuck show that a few dozen small rules cover most everyday mistakes.
How the flow tracer works
The tool treats your app as two pipelines, startup and request, and moves each step from waiting to passed or failed as log lines arrive. There is no AI anywhere: every step has a known "passed" signal, a list of known "failed" signals, and for each failed signal a checklist of causes written as plain code.
Three commands
• fsd run -- <your start command> starts the app, prints your normal logs, and shows a live step panel for startup and then for every request.
• fsd try GET /display sends a request for you and traces it from "sent" to "response", so it can also catch the case where the request never reached the app (wrong port, app not running).
• fsd doctor runs the same checks on your source code without starting the app: bean annotations, packages, duplicate or near-duplicate mappings, database settings, free port.
How it sees each step without changing your code
1. More detailed logs, switched on from outside. When fsd run starts your app it sets LOGGING_LEVEL_ORG_SPRINGFRAMEWORK_WEB=DEBUG, and logging.level.org.hibernate.SQL=DEBUG inside SPRING_APPLICATION_JSON (Spring Boot lowercases logger names taken from environment variables, and org.hibernate.SQL has capitals). Spring Boot reads these on its own, so every request now logs GET "/display", Mapped to DisplayController#display() and Completed 404 NOT_FOUND, and every SQL statement is logged. You don't edit application.properties.
2. One trace per request. Each Spring log line carries its thread name, like [nio-8080-exec-3]. A thread handles one request at a time, so the tool groups lines by thread from GET "/..." to Completed ... and gets one clean trace per request.
3. Stack traces tell how deep it got. When your code throws, the tool keeps only the stack frames from your own package and labels each one controller, service or repository using the source scan. That is how it can say "reached UserService.create, failed inside UserRepository.save".
4. Your source code fills in the details. A scanner reads your .java files once: classes, annotations, packages, injected fields and constructors, and every @GetMapping/@PostMapping path.
Checklist per failure
When a step fails, the tool runs that failure's checklist in order and stops at the first check that fails. It prints the checks it ran, so the person learns how to debug it themselves next time. For example, "required a bean of type 'UserService' that could not be found" runs:
1. Does a class or interface named UserService exist?
2. Does it have @Service, @Component or @Repository?
3. Is it in the same package as your @SpringBootApplication class, or below it?
4. If it is an interface, is there an implementing class with an annotation?
5. Is it switched off by a profile or @ConditionalOn...?
Optional, later: exact flow even when nothing crashes
Logs only show the flow when something goes wrong or a step logs on its own. For bugs where the app runs but returns the wrong thing (an empty list, a null field), a phase 2 add-on would be a tiny dependency, fsd-spring-tracer, that you add to pom.xml while developing. It wraps your controllers, services and repositories and prints one line per step entered and left, with timing. The tool then draws the exact path: controller → service → repository → SQL → response.
Startup steps
A Spring Boot app passes these 10 steps in this order every time it starts. Some steps have no log line of their own; they count as passed when the next step's line appears with no error in between.
#
Step
Passed when the log shows
When it fails, the checklist looks for
1
Build
BUILD SUCCESS, or the app starts printing
Compile errors such as cannot find symbol, missing imports, wrong Java version (invalid target release)
2
Start the app
Starting DemoApplication using Java 21
UnsupportedClassVersionError (built with a newer Java than you run)
3
Load configuration
The following 1 profile is active or No active profile set
Typo in a property key, bad YAML indentation, Failed to bind properties under ...
4
Prepare web server
Tomcat initialized with port 8080
Rare; invalid server.port value
5
Connect to database
HikariPool-1 - Start completed
No URL set, wrong user or password (Access denied), database not running (Connection refused), driver missing from pom.xml
6
Set up entities (JPA)
Initialized JPA EntityManagerFactory
Missing @Entity (Not a managed type), missing @Id, table or column missing when ddl-auto=validate
7
Create beans and inject them
No UnsatisfiedDependencyException before step 9
Missing @Service/@Component, class outside the scanned package, two beans of one type, circular dependency, Could not resolve placeholder for an @Value
8
Register endpoints
No Ambiguous mapping before step 9
Two methods mapped to the same path and HTTP method
9
Open the port
Tomcat started on port 8080
Port already in use (another copy of the app still running)
10
Ready
Started DemoApplication in 3.1 seconds
An exception in your own startup code (CommandLineRunner, @PostConstruct)
What the missing @Service case looks like in the terminal:
Startup
  ✓ 1 Build
  ✓ 2 Start the app
  ✓ 3 Load configuration
  ✓ 4 Prepare web server
  ✓ 5 Connect to database
  ✓ 6 Set up entities
  ✗ 7 Create beans         UserController needs a UserService, none found
  · 8 Register endpoints
  · 9 Open the port
  · 10 Ready

  Checks
  ✓ class UserService exists          (src/main/java/com/demo/service/UserService.java)
  ✗ has @Service / @Component         no annotation found

  Fix: add @Service on the line above "public class UserService" (UserService.java:5)
Request steps
Every HTTP request your frontend or browser sends goes through these 7 steps. With the debug logging from fsd run turned on, each one leaves a trace the tool can follow.
#
Step
Passed when the log shows
When it fails, the checklist looks for
1
Reach the app
GET "/display", parameters={}
Nothing logged at all: app not running, wrong port, frontend calling the wrong host (fsd try knows it sent one)
2
Pass filters
The request reaches the controller lookup
401/403 from Spring Security, Invalid CORS request from a frontend on another port
3
Match a controller
Mapped to DisplayController#display()
No mapping (closest-path "did you mean"), trailing slash, wrong HTTP method (405), duplicate path
4
Read request data
The controller method is entered
Required request body is missing, JSON parse error, missing @RequestParam, "abc" sent where a number is expected, failed @Valid
5
Run your code
No exception from your package's frames
NullPointerException (often from new UserService() instead of injecting it), NoSuchElementException from Optional.get()
6
Run SQL
Hibernate SQL lines with no error after them
Table or column doesn't exist, duplicate key, LazyInitializationException
7
Send the response
Completed 200 OK
Infinite JSON recursion on two-way entity links, No converter for return value, any 4xx or 5xx status
What your typo example looks like with fsd try:
$ fsd try GET /display

GET /display
  ✓ 1 Reach the app        GET "/display"
  ✓ 2 Pass filters
  ✗ 3 Match a controller   No static resource display.
  · 4 Read request data
  · 5 Run your code
  · 6 Run SQL
  ✗ 7 Send the response    404 NOT_FOUND

  Checks
  ✗ a mapping for GET /display exists     none of your 6 mappings match
  ✓ closest mapping                       "/disply/" in DisplayController.java:14
                                          (one letter missing, extra trailing "/")

  Fix: change @GetMapping("/disply/") to @GetMapping("/display"),
       or call /disply/ from the frontend.
  Note: since Spring Boot 3, "/display/" and "/display" are different URLs.
The "closest mapping" check is plain string comparison (edit distance) between the requested path and every path from your source, so it needs no AI.
MVP scope
The first version is a Python CLI that tracks the 10 startup steps and the 7 request steps for Spring Boot, with checklists for about 15 common failures and a sample log test for each. No AI, no network calls, no change to your project.
Stack
• Python 3.11+ with Typer for the commands and Rich for the live step panel and colours.
• Steps and failures as YAML: one file per pipeline listing steps, their "passed" lines and their "failed" patterns. Adding a new mistake means adding a few lines, not changing the engine.
• Checks as small Python functions, each answering one yes/no question about your code, such as has_stereotype_annotation("UserService").
• pytest with a folder of real Spring Boot logs, one per failure, so every check is proven to fire.
Project layout
fsd/
  cli.py             # run / try / doctor
  runner.py          # starts your app, sets debug log env vars, streams lines
  tracker.py         # moves steps to passed/failed, groups request lines by thread
  scanner/spring.py  # reads .java files: classes, annotations, packages, mappings
  checks/spring.py   # one function per yes/no check
  panel.py           # draws the step list and the fix
  session.py         # request traces as JSON lines in the OS temp folder, for fsd try
  client.py          # fsd try: sends one request with the standard library
  pipelines/spring_startup.yaml   # package data, read with importlib.resources
  pipelines/spring_request.yaml
tests/logs/*.log
Build order
1. Runner that starts the app and passes output through unchanged, with the debug env vars set.
2. Startup pipeline: tick steps 1 to 10 off from real log lines, show the panel.
3. Source scanner, then the startup checklists: missing bean, port in use, database, entity.
4. Request pipeline: group lines by thread, map them to steps 1 to 7.
5. fsd try, then the request checklists: 404 "did you mean", 405, bad body, SQL errors, JSON recursion.
6. fsd doctor, reusing the same checks without starting the app.
7. README with a short GIF, publish on GitHub and PyPI.
8. Phase 2: the optional fsd-spring-tracer dependency for bugs that don't crash, then the next stack.
Open choices
No AI is decided. Two choices are still yours, and the plan assumes the defaults below.
Choice
Default
Why
Form of the tool
CLI
Works with any IDE, terminal or framework; an IDE plugin can reuse the same engine later
Stack after Spring Boot
React/Vite frontend calling the Spring API (CORS, proxy, wrong base URL)
It's the other half of a typical full-stack learning project, and CORS is the classic bug that crosses both
Note: the log lines quoted in this doc are from memory of Spring Boot 3.x output. The exact wording gets checked against a real sample app when each step's test is written.