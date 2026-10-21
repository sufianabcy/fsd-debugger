package com.example.config;

import java.time.Clock;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class AppConfig {

	@Bean
	public Clock clock() {
		return Clock.systemUTC();
	}

	@Bean(name = "mainExecutor")
	ExecutorService executor(Clock clock) {
		return Executors.newSingleThreadExecutor();
	}

	@Configuration
	static class Lookups {

		@Bean
		Map<String, List<String>> lookup() {
			return Map.of();
		}
	}
}
