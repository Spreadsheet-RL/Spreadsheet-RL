from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, List, Dict, Optional, Iterator
from functools import partial


def run_concurrent_pure(funcs: Iterator[Callable[[], Any]], concurrency: int = 20) -> List[Any]:
    """
    Execute a list of parameterless functions concurrently using thread pool
    
    Args:
        funcs: Iterator of parameterless functions to execute
        concurrency: Number of concurrent threads, defaults to 20
        
    Returns:
        List of execution results
    """
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(func) for func in funcs]
        results = [f.result() for f in futures]
    return results


def run_concurrent(func: Callable,
                   args: Optional[List] = None,
                   kwargs: Optional[List[Dict]] = None,
                   concurrency: int = 20) -> List[Any]:
    """
    Execute a function concurrently with different args using thread pool
    
    Args:
        func: Function to execute
        args: Optional list of positional args, each element is a list of args
        kwargs: Optional list of keyword args, each element is a dict of kwargs
        concurrency: Number of concurrent threads, defaults to 20
        
    Returns:
        List of execution results
        
    Raises:
        ValueError: If both args and kwargs are None
        ValueError: If args and kwargs have different lengths
    """
    if args is None and kwargs is None:
        raise ValueError("args and kwargs cannot be None at the same time")

    if args is None:
        args = [[]] * len(kwargs)
    if kwargs is None:
        kwargs = [{}] * len(args)

    if len(args) != len(kwargs):
        raise ValueError("Length of args must equal length of kwargs")

    if len(args) == 0:
        return []

    wrapped_funcs = (partial(func, *a, **k) for a, k in zip(args, kwargs))

    return run_concurrent_pure(wrapped_funcs, concurrency)


def trim_slash(url: str) -> str:
    return url.rstrip('/')
