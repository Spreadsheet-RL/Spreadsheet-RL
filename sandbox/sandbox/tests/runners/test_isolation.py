# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import random

from fastapi.testclient import TestClient

from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse, RunStatus
from sandbox.server.server import app

client = TestClient(app)

SERVER_1 = '''
import http.server
import socketserver
import sys
import threading
import requests

# Define the handler to serve the request
class MyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b'Hello, this is a special string!')

# Function to start the server
def start_server():
    try:
        PORT = {port}
        handler = MyHandler
        with socketserver.TCPServer(("", PORT), handler) as httpd:
            httpd.serve_forever()
    except Exception as e:
        print(e)
        sys.exit(1)

# Run the server in a separate thread
server_thread = threading.Thread(target=start_server)
server_thread.daemon = True
server_thread.start()

# Function to make a request to the server and check the response
def test_server_response():
    url = "http://127.0.0.1:{port}"
    response = requests.get(url)
    
    if response.status_code == 200 and "special string" in response.text:
        print("Test Passed: Correct response received.")
    else:
        print("Test Failed: Incorrect response received.")

# Give the server a moment to start
import time
time.sleep({wait_time})

# Test the server response
test_server_response()
'''

SERVER_2 = '''
import http.server
import socketserver
import sys
import threading
import requests

# Define the handler to serve the request
class MyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b'Hello, this is a special string!')

# Function to start the server
def start_server():
    try:
        PORT = {port}
        handler = MyHandler
        with socketserver.TCPServer(("", PORT), handler) as httpd:
            httpd.serve_forever()
    except Exception as e:
        print(e)
        sys.exit(1)

# Run the server in a separate thread
server_thread = threading.Thread(target=start_server)
server_thread.daemon = True
server_thread.start()

# Function to make a request to the server and check the response
def test_server_response():
    url = "http://localhost:{port}"
    response = requests.get(url)
    
    if response.status_code == 200 and "special string" in response.text:
        print("Test Passed: Correct response received.")
    else:
        print("Test Failed: Incorrect response received.")

# Give the server a moment to start
import time
time.sleep({wait_time})

# Test the server response
test_server_response()
'''

NET_1 = '''
import requests

def test_network_access():
    try:
        response = requests.get('https://www.sina.com.cn', timeout=5)
        print(response)
        # print(response.text)
        if response.status_code == 200:
            print("Network access successful.")
        else:
            raise Exception("Network access failed with status code: {}".format(response.status_code))
    except requests.ConnectionError:
        raise Exception("Network access failed. Unable to connect to the external network.")
    except requests.Timeout:
        raise Exception("Network access failed. The request timed out.")
    except Exception as e:
        raise Exception("An error occurred: {}".format(str(e)))

test_network_access()
'''


def test_isolation_network_server_127():
    request = RunCodeRequest(language='python',
                             code=SERVER_1.format(port=random.randint(30000, 60000), wait_time=1),
                             run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    print(result.model_dump_json(indent=2))
    assert result.status == RunStatus.Success
    assert 'Test Passed' in result.run_result.stdout


def test_isolation_network_server_localhost():
    request = RunCodeRequest(language='python',
                             code=SERVER_2.format(port=random.randint(30000, 60000), wait_time=1),
                             run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    print(result.model_dump_json(indent=2))
    assert result.status == RunStatus.Success
    assert 'Test Passed' in result.run_result.stdout


async def test_isolation_network_server_port_conflict():
    request = RunCodeRequest(language='python',
                             code=SERVER_1.format(port=random.randint(30000, 60000), wait_time=2),
                             run_timeout=6)

    def post():
        return client.post('/run_code', json=request.model_dump())

    results = await asyncio.gather(asyncio.to_thread(post), asyncio.to_thread(post))
    for response in results:
        assert response.status_code == 200
        result = RunCodeResponse(**response.json())
        print(result.model_dump_json(indent=2))
        assert result.status == RunStatus.Success
        assert 'Test Passed' in result.run_result.stdout


def test_isolation_network_external_access():
    request = RunCodeRequest(language='python', code=NET_1, run_timeout=20)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    print(result.model_dump_json(indent=2))
    assert result.status == RunStatus.Success
