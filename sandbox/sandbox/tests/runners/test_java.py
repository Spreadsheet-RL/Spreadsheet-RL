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


def test_java_print():
    request = RunCodeRequest(language='java',
                             code='''
    public class Main {
        public static void main(String[] args) {
            System.out.println(123);
        }
    }
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.compile_result.status == CommandRunStatus.Finished
    assert result.run_result.stdout.strip() == '123'


def test_java_compile_error():
    request = RunCodeRequest(language='java',
                             code='''
    public class Main {
        public static void main(String[] args) {
            System.uto.println(123);
        }
    }
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.compile_result.status == CommandRunStatus.Finished
    assert result.compile_result.return_code != 0
    assert result.run_result is None


def test_java_timeout():
    request = RunCodeRequest(language='java',
                             code='''
    public class Main {
        public static void main(String[] args) {
            try {
                Thread.sleep(200);
            } catch (InterruptedException e) {
                System.err.println("The sleeping thread was interrupted: " + e.getMessage());
            }
        }
    }
    ''',
                             run_timeout=0.1)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.TimeLimitExceeded


def test_java_assertion_error():
    request = RunCodeRequest(language='java',
                             code='''
    public class Main {
        public static void main(String[] args) {
            throw new AssertionError("Intentional assertion error");
        }
    }
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.Finished
    assert "java.lang.AssertionError" in result.run_result.stderr


def test_java_assert():
    request = RunCodeRequest(language='java',
                             code='''
    public class Main {
        public static void main(String[] args) {
            assert 1 == 2;
        }
    }
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.Finished


def test_java_external_jar():
    request = RunCodeRequest(language='java',
                             code='''
    import org.javatuples.Pair;
    import org.javatuples.Triplet;

    public class Main {
        public static void main(String[] args) {
            // Creating a Pair of String and Integer
            Pair<String, Integer> pair = Pair.with("apple", 1);

            // Accessing the elements of the Pair
            String fruit = pair.getValue0();
            Integer quantity = pair.getValue1();
            System.out.println("Fruit: " + fruit + ", Quantity: " + quantity);

            // Creating a Triplet of String, Integer, and Double
            Triplet<String, Integer, Double> triplet = Triplet.with("banana", 2, 0.99);

            // Accessing the elements of the Triplet
            String fruitTriplet = triplet.getValue0();
            Integer quantityTriplet = triplet.getValue1();
            Double priceTriplet = triplet.getValue2();
            System.out.println("Fruit: " + fruitTriplet + ", Quantity: " + quantityTriplet + ", Price: " + priceTriplet);
        }
    }
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished
    assert 'Fruit: banana, Quantity: 2, Price: 0.99' in result.run_result.stdout


def test_java_stdin():
    request = RunCodeRequest(language='java',
                             code='''
    import java.util.Scanner;

    public class Main {
        public static void main(String[] args) {
            Scanner scanner = new Scanner(System.in);
            int number = scanner.nextInt();
            System.out.println(number);
        }
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
