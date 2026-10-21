package com.example.model;

import java.math.BigDecimal;

public record Money(BigDecimal amount, String currency) implements Comparable<Money> {

	public Money {
		if (amount == null) {
			throw new IllegalArgumentException("amount");
		}
	}

	@Override
	public int compareTo(Money other) {
		return amount.compareTo(other.amount);
	}
}
