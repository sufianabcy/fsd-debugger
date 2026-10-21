package com.demo.controller;

import java.util.List;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

import com.demo.model.Post;
import com.demo.service.PostService;

@RestController
public class PostController {

	private final PostService postService;

	public PostController(PostService postService) {
		this.postService = postService;
	}

	@GetMapping("/posts")
	public List<Post> list() {
		return postService.findAll();
	}

	@PostMapping("/users/{userId}/posts")
	public Post create(@PathVariable Long userId, @RequestBody Post post) {
		return postService.create(userId, post);
	}

}
