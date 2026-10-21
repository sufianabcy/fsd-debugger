package com.demo.service;

import java.util.List;

import org.springframework.stereotype.Service;

import com.demo.model.Post;
import com.demo.model.User;
import com.demo.repository.PostRepository;
import com.demo.repository.UserRepository;

@Service
public class PostService {

	private final PostRepository postRepository;

	private final UserRepository userRepository;

	public PostService(PostRepository postRepository, UserRepository userRepository) {
		this.postRepository = postRepository;
		this.userRepository = userRepository;
	}

	public List<Post> findAll() {
		return postRepository.findAll();
	}

	public Post create(Long userId, Post post) {
		User user = userRepository.findById(userId).orElseThrow();
		post.setUser(user);
		return postRepository.save(post);
	}

}
