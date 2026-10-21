package com.example.web;

import static org.springframework.web.bind.annotation.RequestMethod.DELETE;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping(value = "/examples")
public class MappingExamples {

	@GetMapping
	public String index() {
		return "index";
	}

	@GetMapping(value = "/items/{id}")
	public String item(@PathVariable("id") Long id) {
		return "item";
	}

	@PostMapping(path = "items")
	public String create(@RequestBody String body) {
		return "created";
	}

	@RequestMapping(value = "/search", method = RequestMethod.GET)
	public String search(@RequestParam(defaultValue = "a,b") String q) {
		return "search";
	}

	@RequestMapping(path = {"/a", "/b"}, method = {RequestMethod.PUT, RequestMethod.PATCH})
	public String update() {
		return "updated";
	}

	@RequestMapping("/any")
	public String any() {
		return "any";
	}

	@RequestMapping(value = "/static-import", method = DELETE)
	public String remove() {
		return "removed";
	}

	@DeleteMapping("/items/{id:\\d+}")
	public String removeItem() {
		return "removed";
	}

	@GetMapping(
			value = "/multi-line",
			produces = "application/json")
	public String multiLine() {
		return "{}";
	}

	@GetMapping("/joined" + "/path")
	public String joined() {
		return "joined";
	}

	@GetMapping(Paths.REPORTS)
	public String constant() {
		return "constant";
	}

	@GetMapping("/disply/")
	public String typo() {
		return "typo";
	}

	// @GetMapping("/commented-out")
	public String helper() {
		String fake = "@GetMapping(\"/in-a-string\") { ";
		return fake;
	}
}

class Paths {

	static final String REPORTS = "/reports";
}
