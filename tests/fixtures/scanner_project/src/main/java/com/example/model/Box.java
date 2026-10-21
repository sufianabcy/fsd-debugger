package com.example.model;

import java.io.Serializable;
import java.util.function.Supplier;

public class Box<T extends Comparable<T>> extends AbstractBox<T> implements Serializable, Supplier<T> {

	@Override
	public T get() {
		return null;
	}
}

abstract class AbstractBox<T> {
}
