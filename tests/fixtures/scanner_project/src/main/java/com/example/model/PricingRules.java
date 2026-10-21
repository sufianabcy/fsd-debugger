package com.example.model;

import java.math.BigDecimal;
import java.time.Clock;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

@Component
public record PricingRules(Clock clock, @Value("${pricing.rate}") BigDecimal rate) {
}
