package com.demo.controller;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class DisplayController {

	@GetMapping("/display")
	public String display() {
		return "Hello from DisplayController";
	}

}
