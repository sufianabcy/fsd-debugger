package com.example.service;

import org.springframework.stereotype.Service;

@Service("greeter")
public class EnglishGreetingService implements GreetingService {

	@Override
	public String greet(String name) {
		return "Hello " + name;
	}
}
