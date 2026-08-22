"""Simple greeting module.

Provides functionality to print personalized greeting messages.
"""


def greet(name: str) -> None:
    """Print a greeting message for the given name.

    Args:
        name: The name to include in the greeting.

    Returns:
        None
    """
    message: str = f"Hello, {name}"
    print(message)


def main() -> None:
    """Entry point for the greeting module."""
    greet("Claude")


if __name__ == "__main__":
    main()
