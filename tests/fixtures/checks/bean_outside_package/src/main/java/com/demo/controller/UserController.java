package com.demo.controller;

import org.springframework.web.bind.annotation.RestController;

import com.demo.service.UserService;

@RestController
public class UserController {

	private final UserService userService;

	public UserController(UserService userService) {
		this.userService = userService;
	}
}
