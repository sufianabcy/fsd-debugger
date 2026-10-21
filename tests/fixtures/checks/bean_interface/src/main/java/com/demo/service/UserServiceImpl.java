package com.demo.service;

public class UserServiceImpl implements UserService {

	@Override
	public String greet(String name) {
		return "Hello " + name;
	}
}
