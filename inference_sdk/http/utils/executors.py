import asyncio
from enum import Enum
from functools import partial
from multiprocessing.pool import ThreadPool
from typing import List, Tuple, Union

import aiohttp
import backoff
import requests
from aiohttp import ClientConnectionError, ClientResponse
from requests import Response

from inference_sdk.http.utils.iterables import make_batches
from inference_sdk.http.utils.request_building import RequestData
from inference_sdk.http.utils.requests import api_key_safe_raise_for_status

RETRYABLE_STATUS_CODES = {429, 503}


class RequestMethod(Enum):
    GET = "get"
    POST = "post"


REQUEST_METHOD2FUNCTION = {
    RequestMethod.GET: requests.get,
    RequestMethod.POST: requests.post,
}


def execute_requests_packages(
    requests_data: List[RequestData],
    request_method: RequestMethod,
    max_concurrent_requests: int,
) -> List[Response]:
    requests_data_packages = make_batches(
        iterable=requests_data,
        batch_size=max_concurrent_requests,
    )
    results = []
    for requests_data_package in requests_data_packages:
        responses = make_parallel_requests(
            requests_data=requests_data_package,
            request_method=request_method,
        )
        results.extend(responses)
    for response in results:
        api_key_safe_raise_for_status(response=response)
    return results


def make_parallel_requests(
    requests_data: List[RequestData],
    request_method: RequestMethod,
) -> List[Response]:
    workers = len(requests_data)
    make_request_closure = partial(make_request, request_method=request_method)
    with ThreadPool(processes=workers) as pool:
        return pool.map(
            make_request_closure,
            iterable=requests_data,
        )


@backoff.on_predicate(
    backoff.constant,
    predicate=lambda r: r.status_code in RETRYABLE_STATUS_CODES,
    max_tries=3,
    interval=1,
)
@backoff.on_exception(
    backoff.constant,
    exception=ConnectionError,
    max_tries=3,
    interval=1,
)
def make_request(request_data: RequestData, request_method: RequestMethod) -> Response:
    function = REQUEST_METHOD2FUNCTION[request_method]
    return function(
        request_data.url,
        headers=request_data.headers,
        params=request_data.parameters,
        data=request_data.data,
        json=request_data.payload,
    )


async def execute_requests_packages_async(
    requests_data: List[RequestData],
    request_method: RequestMethod,
    max_concurrent_requests: int,
) -> List[Union[dict, bytes]]:
    requests_data_packages = make_batches(
        iterable=requests_data,
        batch_size=max_concurrent_requests,
    )
    results = []
    for requests_data_package in requests_data_packages:
        responses = await make_parallel_requests_async(
            requests_data=requests_data_package,
            request_method=request_method,
        )
        results.extend(responses)
    return results


async def make_parallel_requests_async(
    requests_data: List[RequestData],
    request_method: RequestMethod,
) -> List[Union[dict, bytes]]:
    async with aiohttp.ClientSession() as session:
        make_request_closure = partial(
            make_request_async,
            request_method=request_method,
            session=session,
        )
        coroutines = [make_request_closure(data) for data in requests_data]
        responses = list(await asyncio.gather(*coroutines))
        return [r[1] for r in responses]


@backoff.on_predicate(
    backoff.constant,
    predicate=lambda r: r[0] in RETRYABLE_STATUS_CODES,
    max_tries=3,
    interval=1,
)
@backoff.on_exception(
    backoff.constant,
    exception=ClientConnectionError,
    max_tries=3,
    interval=1,
)
async def make_request_async(
    request_data: RequestData,
    request_method: RequestMethod,
    session: aiohttp.ClientSession,
) -> Tuple[int, Union[bytes, dict]]:
    method = session.get if request_method is RequestMethod.GET else session.post
    parameters_serialised = None
    if request_data.parameters is not None:
        parameters_serialised = {
            name: str(value)
            if not issubclass(type(value), list)
            else [str(e) for e in value]
            for name, value in request_data.parameters.items()
        }
    async with method(
        request_data.url,
        headers=request_data.headers,
        params=parameters_serialised,
        data=request_data.data,
        json=request_data.payload,
    ) as response:
        try:
            response_data = await response.json()
        except:
            response_data = await response.read()
        if response_is_not_retryable_error(response=response):
            response.raise_for_status()
        return response.status, response_data


def response_is_not_retryable_error(response: ClientResponse) -> bool:
    return response.status != 200 and response.status not in RETRYABLE_STATUS_CODES
