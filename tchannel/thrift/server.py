# Copyright (c) 2015 Uber Technologies, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import absolute_import

import sys
from collections import namedtuple

from tornado import gen

from tchannel import Response
from tchannel.tornado.request import TransportMetadata
from tchannel.tornado.response import (
    StatusCode,
    Response as DeprecatedResponse,
)

from ..serializer.thrift import ThriftSerializer


def register(dispatcher, service_module, handler, method=None, service=None):
    """Registers a Thrift service method with the given RequestDispatcher.

    .. code-block:: python

        # For,
        #
        #   service HelloWorld { string hello(1: string name); }

        import tchannel.thrift
        import HelloWorld

        def hello(request, response, tchannel):
            name = request.args.name
            response.write_result("Hello, %s" % name)

        dispatcher = RequestDispatcher()
        tchannel.thrift.register(dispatcher, HelloWorld, hello)

    :param dispatcher:
        TChannel dispatcher with which the Thrift service will be registered.
    :param service_module:
        The service module generated by Thrift. This module contains the
        service ``Iface``, ``Client``, ``Processor``, etc. classes.
    :param handler:
        A function implementing the request handler. The function must accept
        a ``request``, a ``response``, and a ``tchannel``.
    :param service:
        Thrift service name. This is the `service` name specified in the
        Thrift IDL. If omitted, it is automatically determined based on the
        name of ``service_module``.
    :param method:
        Name of the method. Defaults to the name of the ``handler`` function.
    """
    if not service:
        service = service_module.__name__.rsplit('.', 1)[-1]
    if not method:
        method = handler.__name__
    assert service, 'A service name could not be determined'
    assert method, 'A method name could not be determined'

    assert hasattr(service_module.Iface, method), (
        "Service %s doesn't define method %s" % (service, method)
    )
    assert hasattr(service_module, method + '_result'), (
        "oneway methods are not yet supported"
    )

    endpoint = '%s::%s' % (service, method)
    args_type = getattr(service_module, method + '_args')
    result_type = getattr(service_module, method + '_result')

    # only wrap the handler if we arent using the new server api
    if not dispatcher._handler_returns_response:
        handler = build_handler(result_type, handler)
    else:
        handler = new_build_handler(result_type, handler)

    dispatcher.register(
        endpoint,
        handler,
        ThriftSerializer(args_type),
        ThriftSerializer(result_type)
    )
    return handler


def new_build_handler(result_type, f):
    @gen.coroutine
    def handler(request):
        result = ThriftResponse(result_type())
        response = Response()

        try:
            response = yield gen.maybe_future(f(request))

            # if no return then use empty response
            if response is None:
                response = Response()

            # if not a response, then it's just the body, create resp
            if not isinstance(response, Response):
                response = Response(response)
        except Exception:
            result.write_exc_info(sys.exc_info())
        else:
            result.write_result(response.body)

        response.body = result.result

        raise gen.Return(response)
    return handler



def build_handler(result_type, f):
    @gen.coroutine
    def handler(request, response, tchannel):
        req = yield ThriftRequest._from_raw_request(request)
        res = ThriftResponse(result_type())
        try:
            # TODO: It would be nice if we could wait until write_result was
            # called or an exception was thrown instead of waiting for the
            # function to return. This would allow for use cases where the
            # implementation returns the result early but still does some work
            # after that.
            result = yield gen.maybe_future(f(req, res, tchannel))
        except Exception:
            res.write_exc_info(sys.exc_info())
        else:
            if not res.finished and result is not None:
                # The user never called write_result or threw an
                # exception. The result was most likely returned by the
                # function.
                res.write_result(result)
        response.code = res.code
        response.write_header(res.headers)
        response.write_body(res.result)
    return handler


class ThriftRequest(namedtuple('_Request', 'headers args transport')):
    """Represents a Thrift call request.

    Makes the following attributes available:

    ``headers``
        The application-level headers passed in for this request.

    ``args``
        An object containing the parameters for the Thrift call. The object's
        attributes will have the same name as the parameters defined in the
        Thrift IDL.

    ``transport``
        Provides access to the transport metadata. Among other things, this
        includes,

        ``headers``
            The transport-level headers
        ``ttl``
            TTL for this request in milliseconds. The caller expects a
            response within this period. If this time is exceeded, the timeout
            may kick in at the call site or in the forwarding layer.
    """

    @classmethod
    @gen.coroutine
    def _from_raw_request(cls, request):
        call_headers = yield request.get_header()
        call_args = yield request.get_body()
        transport_metadata = TransportMetadata.from_request(request)
        raise gen.Return(
            cls(
                headers=call_headers,
                args=call_args,
                transport=transport_metadata,
            )
        )



class ThriftResponse(object):
    """Represents a response to a Thrift call."""

    __slots__ = ('headers', 'result', 'finished', 'code')

    def __init__(self, result=None):
        self.headers = {}
        self.result = result
        self.finished = False
        self.code = StatusCode.ok

    def write_header(self, name, value):
        """Add a header to be written in the response."""
        self.headers[name] = value

    def write_result(self, result):
        """Send back the result of this call.

        Only one of this and `write_exc_info` may be called.

        :param result:
            Return value of the call
        """
        assert not self.finished, "Already sent a response"

        spec = self.result.thrift_spec[0]
        if result is not None:
            assert spec, "Tried to return a result for a void method."
            setattr(self.result, spec[2], result)

        self.finished = True

    def write_exc_info(self, exc_info=None):
        """Write exception information to the response.

        Only one of this and ``write_result`` may be called.

        :param exc_info:
            3-tuple of exception information. If omitted, the last exception
            will be retrieved using ``sys.exc_info()``.
        """
        exc_info = exc_info or sys.exc_info()
        exc = exc_info[1]
        self.code = StatusCode.error
        for spec in self.result.thrift_spec[1:]:
            if spec and isinstance(exc, spec[3][0]):
                assert not self.finished, "Already sent a response"

                setattr(self.result, spec[2], exc)
                self.finished = True
                return

        # Re-raise the exception (with the same traceback) if it didn't match.
        raise exc_info[0], exc_info[1], exc_info[2]
        # TODO for unrecognized exceptions, do we want to send back a
        # Thrift-level TApplicationException instead? Currently this will send
        # a TChannel-level protocol error.
