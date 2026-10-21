package com.example.service;

import java.util.ArrayList;
import java.util.List;

import lombok.NonNull;
import lombok.RequiredArgsConstructor;

import org.springframework.stereotype.Service;

@Service
@RequiredArgsConstructor
public class LombokRequired {

	private static final String PREFIX = "x";

	private final GreetingService greetingService;

	private String notFinal;

	private final List<String> alreadySet = new ArrayList<>();

	@NonNull
	private ReportService reportService;
}
