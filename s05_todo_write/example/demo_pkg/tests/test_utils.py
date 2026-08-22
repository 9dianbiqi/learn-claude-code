"""Tests for demo_pkg.utils."""

from demo_pkg.utils import greet, add, multiply


class TestGreet:
    def test_greet_default(self):
        assert greet("World") == "Hello, World!"

    def test_greet_empty_string(self):
        assert greet("") == "Hello, !"

    def test_greet_with_spaces(self):
        assert greet("Python Dev") == "Hello, Python Dev!"


class TestAdd:
    def test_add_positive(self):
        assert add(2, 3) == 5

    def test_add_negative(self):
        assert add(-1, -2) == -3

    def test_add_zero(self):
        assert add(0, 0) == 0

    def test_add_mixed_signs(self):
        assert add(-5, 10) == 5


class TestMultiply:
    def test_multiply_positive(self):
        assert multiply(3, 4) == 12

    def test_multiply_by_zero(self):
        assert multiply(5, 0) == 0

    def test_multiply_negative(self):
        assert multiply(-2, 3) == -6

    def test_multiply_both_negative(self):
        assert multiply(-2, -3) == 6
