import pytest
from agent_runtime.docker_transport import recv


@pytest.mark.parametrize("pipe,code", [(True,109),(True,5),(False,109)])
def test_only_named_pipe_broken_pipe_is_eof(pipe,code):
    class ReadError(Exception):
        winerror=code
    class Socket:
        def recv(self,size): raise ReadError('read')
    if pipe: Socket.__module__='docker.transport.npipesocket'
    if pipe and code==109:
        assert recv(Socket(),8)==b''
    else:
        with pytest.raises(ReadError): recv(Socket(),8)


def test_bytes_preserved():
    class Socket:
        def recv(self,size): return b'data'[:size]
    assert recv(Socket(),3)==b'dat'
