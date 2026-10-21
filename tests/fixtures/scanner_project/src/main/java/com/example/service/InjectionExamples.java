package com.example.service;

import java.util.List;
import java.util.Optional;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

@Service
public class InjectionExamples {

	@Autowired
	private GreetingService greetingService;

	@Autowired(required = false) private Optional<AuditLog> auditLog;

	private ReportService notInjected;

	@Value("${app.name}")
	private String appName;

	private final Runnable task = () -> {
		System.out.println("not a member");
	};

	public InjectionExamples() {
		this(null, 0, List.of());
	}

	@Autowired
	public InjectionExamples(ReportService reportService,
			@Value("${app.size:10}") int size,
			final List<Plugin> plugins) {
	}
}

interface Plugin {
}
