package com.example.model;

import java.util.function.Supplier;

public enum Status implements Supplier<String> {
	ACTIVE("a") {
		@Override
		public String get() {
			return value.length() > 1 ? "x" : "y";
		}
	},
	INACTIVE("i");

	final String value;

	Status(String value) {
		this.value = value;
	}

	public String get() {
		return value;
	}
}
