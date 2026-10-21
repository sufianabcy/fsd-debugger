package com.demo.service;

import java.util.List;

import org.springframework.stereotype.Service;

import com.demo.model.User;
import com.demo.repository.UserRepository;

@Service
public class UserService {

	private final UserRepository userRepository;

	public UserService(UserRepository userRepository) {
		this.userRepository = userRepository;
	}

	public List<User> findAll() {
		return userRepository.findAll();
	}

	public User create(User user) {
		return userRepository.save(user);
	}

	public User findById(Long id) {
		return userRepository.findById(id).orElseThrow();
	}

	public List<User> findByName(String name) {
		return userRepository.findAll().stream().filter(user -> name.equals(user.getName())).toList();
	}

}
