package com.example;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/*
 * Comments and strings in this file mention code that must not count:
 * @Service class FakeInBlockComment { }
 */
@SpringBootApplication(scanBasePackages = "com.example")
public class ExampleApplication {

	// @Component class FakeInLineComment { }

	private static final String NOT_A_CLASS = "class FakeInString { @GetMapping(\"/fake\") }";
	private static final char OPEN_BRACE = '{';
	private static final String TEXT_BLOCK = """
			record FakeInTextBlock() { }
			""";

	public static void main(String[] args) {
		SpringApplication.run(ExampleApplication.class, args);
	}
}
