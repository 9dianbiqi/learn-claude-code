"""Normalize Docker SDK named-pipe EOF; callers still validate frames/exits."""


def recv(connection, size):
    try:
        return connection.recv(size)
    except Exception as exc:
        is_pipe = any(cls.__module__ == "docker.transport.npipesocket"
                      for cls in type(connection).__mro__)
        if is_pipe and getattr(exc, "winerror", None) == 109:
            return b""
        raise
