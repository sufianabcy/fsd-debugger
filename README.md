# fsd

**A step-by-step flow tracer for Spring Boot.** fsd starts your app, follows it
through startup and through every request using its own logs, and when a step
fails it checks your source code for the usual causes and tells you the fix:
which file, which line, what to add.

It is made for people learning Spring Boot. When Spring tells you
`Parameter 0 of constructor in UserController required a bean of type
'UserService' that could not be found`, fsd tells you
`add @Service on the line above "public class UserService" (UserService.java:10)`.

- **No AI, no network.** Every diagnosis is a fixed rule: a log pattern plus a
  small check of your code. The same mistake always gives the same answer.
- **Read-only.** fsd never changes your project's files.
- **Your output stays as it is.** Your app's logs are printed unchanged; fsd
  adds its panel after them, on stderr.

## Install

You need Python 3.11 or newer. [pipx](https://pipx.pypa.io) installs fsd as a
command of its own, away from your other Python packages:

```sh
pipx install fsd
```

Until fsd is on PyPI, install it from GitHub:

```sh
pipx install git+https://github.com/sufianabcy/fsd-debugger.git
```

Check that it works with `fsd --help`.

## The three commands

Run them in your Spring Boot project's folder, the one with `pom.xml` or
`build.gradle`.

### `fsd run`: start your app and watch each step

Put your usual start command after `--`:

```sh
fsd run -- ./mvnw spring-boot:run
```

Your app starts and prints its logs as always. fsd turns on Spring's request
and SQL debug logging from outside (environment variables only, nothing in
`application.properties`), and once the app is ready it shows the 10 startup
steps:

```
╭─ fsd · Startup ──────────────────────────────────────────╮
│ ✓  1 Build                                               │
│ ✓  2 Start the app       DemoAppApplication, Java 21.0.9 │
│ ✓  3 Load configuration                                  │
│ ✓  4 Prepare web server  port 8080                       │
│ ✓  5 Connect to database                                 │
│ ✓  6 Set up entities                                     │
│ ✓  7 Create beans                                        │
│ ✓  8 Register endpoints  9 mappings                      │
│ ✓  9 Open the port       port 8080                       │
│ ✓ 10 Ready               in 2.19 s                       │
╰──────────────────────────────────────────────────────────╯
```

When startup fails, you see where it stopped and what fsd found in your code.
Here `@Service` was removed from `UserService`:

```
╭─ fsd · Startup ─────────────────────────────────────────────────────────╮
│ ✓  1 Build                                                              │
│ ✓  2 Start the app       DemoAppApplication, Java 21.0.9                │
│ ✓  3 Load configuration                                                 │
│ ✓  4 Prepare web server  port 8080                                      │
│ ✓  5 Connect to database                                                │
│ ✓  6 Set up entities                                                    │
│ ✗  7 Create beans        UserController needs a UserService, none found │
│ ·  8 Register endpoints                                                 │
│ ·  9 Open the port                                                      │
│ · 10 Ready                                                              │
╰─────────────────────────────────────────────────────────────────────────╯
  Checks
    ✓ class UserService exists
      src/main/java/com/demo/service/UserService.java:10
    ✗ UserService has no @Service, @Component or other bean annotation
      src/main/java/com/demo/service/UserService.java:10

  Fix: add @Service on the line above "public class UserService" (UserService.java:10)
```

The checks are listed in the order they ran, so you also see how to track the
problem down yourself next time.

While the app runs, every request gets one line, or a full trace when it fails.
Stop the app with Ctrl+C as usual.

### `fsd try`: send a request and trace it

With `fsd run` running in one terminal, send a request from another:

```sh
fsd try GET /user/1
```

```
╭─ fsd · GET /user/1 ─────────────────────────────────╮
│ ✓ 1 Reach the app      GET "/user/1"                │
│ ✓ 2 Pass filters                                    │
│ ✗ 3 Match a controller nothing is mapped to /user/1 │
│ · 4 Read request data                               │
│ · 5 Run your code                                   │
│ · 6 Run SQL                                         │
│ ✗ 7 Send the response  404 NOT_FOUND                │
╰─────────────────────────────────────────────────────╯
  Checks
    ✓ no mapping has the path /user/1 with another HTTP method
    ✓ no mapping differs from /user/1 only by the / at the end
    ✗ GET /users/{id} is mapped: it says "users" where the request has "user"
      src/main/java/com/demo/controller/UserController.java:36

  Fix: call /users/1, or change @RequestMapping("/users") to @RequestMapping("/user")
  (UserController.java:17; every path in UserController starts with it)
```

A request that works prints the steps with a ✓ each, including the SQL it ran.
Send a JSON body with `--body '{"name": "Ada"}'`, and use `--port` if your app
is not on 8080. Because fsd sent the request itself, it can also tell you when
nothing answered at all: app not running, or the wrong port.

### `fsd doctor`: check your code without starting the app

```sh
fsd doctor            # this folder
fsd doctor path/to/app
```

It runs the checks that need only your source code:

- every class that asks for a bean (constructor, `@Autowired` field, `@Bean`
  method parameter) has one to get
- every bean, repository and entity is in a package Spring Boot scans
- no two methods map the same path and HTTP method, and no two paths are a
  likely typo apart (`/users` and `/user`, `/posts` and `/posts/`)
- entities use `jakarta.persistence` and have an `@Id`, and repositories store
  `@Entity` classes
- JPA in `pom.xml` or `build.gradle` comes with a database: a
  `spring.datasource.url`, or an in-memory one such as H2
- the port in `server.port` (8080 by default) is free

```
fsd doctor · /Users/you/code/demo-app

Beans
  ✓ every bean is in com.demo or a package below it (7 beans)
  ✗ UserController asks for UserService: UserService has no @Service, @Component or
    other bean annotation
    src/main/java/com/demo/service/UserService.java:10
    Fix: add @Service on the line above "public class UserService" (UserService.java:10)

Mappings
  ✓ no two mappings have the same path and HTTP method (7 mappings)
  ✓ no two paths differ only by a typo or a / at the end

Entities
  ✓ every entity uses jakarta.persistence, has an @Id and is in a package Spring Boot
    scans (2 entities)
  ✓ every repository stores an @Entity class (2 repositories)

Database
  ✓ no spring.datasource.url, so Spring Boot starts an in-memory H2 database
    pom.xml:46

Port
  ✓ port 8080 is free (the default; server.port is not set)

✗ 1 problem found, 7 checks passed
```

It exits with 1 when it finds a problem and 0 when it finds none, so you can
run it in CI or a git hook.

## What it knows

**Startup:** compile errors (with similar names when a method or class is
misspelled), bad property values, database not running, missing `@Entity` or
`@Id`, `javax.persistence` imports, missing beans (no annotation, outside the
scanned package, an interface with no annotated implementation, a library type
such as `RestTemplate`), duplicate mappings, port already in use.

**Requests:** 404 with the closest mapping ("did you mean"), a `/` too many or
too few at the end, singular and plural (`/user` and `/users`), 405 wrong HTTP
method, missing or malformed JSON body, a JSON field of the wrong type, a
missing `@RequestParam`, text where a number is expected, exceptions in your
code (with the controller → service → repository path it took), SQL errors
(missing table or column, duplicate key), lazy loading outside a session, and
endless JSON from two entities that point at each other.

## Supported versions

- **Spring Boot 4.1** is what fsd is tested on: the logs in `tests/logs/` come
  from a real Spring Boot 4.1 app (`samples/demo-app`, Java 21), one per failure.
- **Spring Boot 3** wording is covered where it differs and fsd knows it
  (Hibernate 6 lazy-loading and Jackson 2 recursion messages), by tests that
  rewrite the Boot 4 log lines into the older wording. Other patterns are
  written to accept 3.x output but have not been checked against a real
  Spring Boot 3 app.
- Spring MVC (servlet) apps with Tomcat, built with Maven or Gradle.
- Python 3.11 or newer, on macOS, Linux and Windows.

## Known limits

- **Java source only.** fsd reads `.java` files under `src/main/java` with
  pattern matching, not a full Java parser. Kotlin and Groovy source is not
  read, and a mapping path built from a constant (`@GetMapping(Paths.USERS)`)
  is shown as unknown.
- **Run it in the app's folder.** In a multi-module build, that is the module
  with the `@SpringBootApplication` class.
- **No checklist yet** for Spring Security (401, 403), CORS, WebFlux, circular
  dependencies, two beans of one type, or `@Profile` and `@ConditionalOn...`
  switching a bean off. Those failures still show as a failed step, with
  Spring's own message.
- **Build files are read simply.** Dependencies from a parent POM or a Gradle
  version catalog (`libs.xyz`) are not seen by `fsd doctor`.
- **Bugs that don't fail.** fsd follows what the logs show, so a request that
  returns 200 with the wrong data looks fine to it.
- **Which program holds a port** is named with `lsof` on macOS and Linux. On
  Windows fsd tells you the port is taken, but not by which program.
- **Old Windows consoles:** the ✓ and ✗ need a UTF-8 terminal, as in Windows
  Terminal. If you see an encoding error, set `PYTHONUTF8=1`.

## Contributing

```sh
pip install -e ".[dev]"
pytest -q
```

`docs/design.md` explains how fsd works. Every step, failure pattern and check
has a test that uses a real log from `tests/logs/`.
