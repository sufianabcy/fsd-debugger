package com.example.service;

import lombok.AllArgsConstructor;

import org.springframework.stereotype.Component;

@Component
@AllArgsConstructor
public class LombokAll {

	private static int created;

	private GreetingService greetingService;

	private final String fixed = "fixed";

	private final AuditLog auditLog;
}
