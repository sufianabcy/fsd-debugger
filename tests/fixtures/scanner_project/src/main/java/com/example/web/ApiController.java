package com.example.web;

import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestMethod;
import org.springframework.web.bind.annotation.ResponseBody;

@Controller
@RequestMapping(path = {"/api", "/v1"})
public class ApiController {

	@GetMapping
	@ResponseBody
	public String root() {
		return "api";
	}

	@PostMapping("status/")
	@ResponseBody
	public String status() {
		return "ok";
	}
}

@Controller
@RequestMapping(value = "/legacy", method = RequestMethod.GET)
class LegacyController {

	@RequestMapping("/page")
	public String page() {
		return "page";
	}

	@PostMapping("/form")
	public String form() {
		return "form";
	}
}

// A client for another service: its mappings are not served by this app.
interface RemoteClient {

	@GetMapping("/remote")
	String fetch();
}
