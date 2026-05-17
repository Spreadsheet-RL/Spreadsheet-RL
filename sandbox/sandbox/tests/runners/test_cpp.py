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

from fastapi.testclient import TestClient

from sandbox.runners import CommandRunStatus
from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse, RunStatus
from sandbox.server.server import app

client = TestClient(app)


def test_cpp_print():
    request = RunCodeRequest(language='cpp',
                             code='''
    #include <iostream>

    int main() {
        std::cout << "123" << std::endl;
        return 0;
    }
    ''',
                             run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.compile_result.status == CommandRunStatus.Finished
    assert result.run_result.stdout.strip() == '123'


def test_cpp_timeout():
    request = RunCodeRequest(language='cpp',
                             code='''
    #include <iostream>
    #include <chrono>
    #include <thread>

    int main() {
        std::this_thread::sleep_for(std::chrono::milliseconds(1000));
        return 0;
    }
    ''',
                             run_timeout=0.5,
                             compile_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.TimeLimitExceeded


def test_cpp_assertion_error():
    request = RunCodeRequest(language='cpp',
                             code='''
    #include <iostream>
    #include <cassert>

    int main() {
        assert(1 == 2);
        return 0;
    }
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.Finished
    assert "Assertion" in result.run_result.stderr


def test_cpp_compile_error():
    request = RunCodeRequest(language='cpp',
                             code='''
    int main() {
        object i = ?;
        return 0;
    }
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.compile_result.status == CommandRunStatus.Finished
    assert result.compile_result.return_code != 0
    assert result.run_result is None


def test_cpp_stdin():
    request = RunCodeRequest(language='cpp',
                             code='''
    #include <iostream>

    int main() {
        int num;
        std::cin >> num;
        std::cout << num << std::endl;
        return 0;
    }
    ''',
                             run_timeout=5,
                             stdin='65535')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished
    assert result.run_result.stdout == '65535\n'


def test_cpp_pthread():
    code = """
#include <memory>
#include <queue>
#include <mutex>
#include <condition_variable>
#include <iostream>
#include <thread>
#include <vector>
#include <optional>

template<typename T>
class ThreadSafeQueue {
public:
    ThreadSafeQueue() = default;

    template<class U>
    auto push(U&& val) -> std::enable_if_t<std::is_same<T, typename std::decay<U>::type>::value> {
        std::unique_lock<std::mutex> lock(mutex_);
        queue_.push(std::forward<U>(val));
        cv_.notify_one();
    }

    std::optional<T> pop() {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait(lock, [this]() { return !queue_.empty(); });
        T value = std::move(queue_.front());
        queue_.pop();
        return value;
    }

    bool empty() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return queue_.empty();
    }

private:
    mutable std::mutex mutex_;
    std::condition_variable cv_;
    std::queue<T> queue_;
};



void producer(ThreadSafeQueue<int>& queue, int id) {
    for (int i = 0; i < 3; ++i) {
        std::cout << "Producer " << id << " pushing " << i << std::endl;
        queue.push(i);
    }
}

void producer_move(ThreadSafeQueue<std::unique_ptr<int>>& queue, int id) {
    for (int i = 0; i < 3; ++i) {
        std::cout << "Producer " << id << " pushing " << i << std::endl;
        queue.push(std::move(std::make_unique<int>(i)));
    }
}

void consumer(ThreadSafeQueue<int>& queue, int id) {
    for (int i = 0; i < 3; ++i) {
        auto value = queue.pop();
        std::cout << "Consumer " << id << " popped " << *value << std::endl;
    }
}

void consumer_move(ThreadSafeQueue<std::unique_ptr<int>>& queue, int id) {
    for (int i = 0; i < 3; ++i) {
        auto value = queue.pop();
        std::cout << "Consumer " << id << " popped " << *(*value) << std::endl;
    }
}

int main() {
    {
        ThreadSafeQueue<int> queue;

        std::thread producer1(producer, std::ref(queue), 1);
        std::thread producer2(producer, std::ref(queue), 2);
        std::thread consumer1(consumer, std::ref(queue), 1);
        std::thread consumer2(consumer, std::ref(queue), 2);

        producer1.join();
        producer2.join();
        consumer1.join();
        consumer2.join();
    }

    {
        ThreadSafeQueue<std::unique_ptr<int>> queue;

        std::thread producer1(producer_move, std::ref(queue), 1);
        std::thread producer2(producer_move, std::ref(queue), 2);
        std::thread consumer1(consumer_move, std::ref(queue), 1);
        std::thread consumer2(consumer_move, std::ref(queue), 2);

        producer1.join();
        producer2.join();
        consumer1.join();
        consumer2.join();
    }


    return 0;
}
    """
    request = RunCodeRequest(language='cpp', code=code, run_timeout=2)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished
