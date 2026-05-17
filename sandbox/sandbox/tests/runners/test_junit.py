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


def test_junit_pass():
    request = RunCodeRequest(language='junit',
                             code='''
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

public class PhoneNumberCreator {
    public static String createPhoneNumber(int[] numbers) {
        StringBuilder phoneNumber = new StringBuilder("(");
        for (int i = 0; i < numbers.length; i++) {
            if (i == 3) {
                phoneNumber.append(") ");
            } else if (i == 6) {
                phoneNumber.append("-");
            }
            phoneNumber.append(numbers[i]);
        }
        return phoneNumber.toString();
    }
}

class PhoneNumberCreatorTest {
    @Test
    void testCreatePhoneNumber2() {
        assertEquals("(987) 654-3210", PhoneNumberCreator.createPhoneNumber(new int[]{9,8,7,6,5,4,3,2,1,0}));
    }

    @Test
    void testCreatePhoneNumber3() {
        assertEquals("(111) 111-1111", PhoneNumberCreator.createPhoneNumber(new int[]{1,1,1,1,1,1,1,1,1,1}));
    }

    @Test
    void testCreatePhoneNumber4() {
        assertEquals("(999) 999-9999", PhoneNumberCreator.createPhoneNumber(new int[]{9,9,9,9,9,9,9,9,9,9}));
    }

    @Test
    void testCreatePhoneNumber5() {
        assertEquals("(123) 456-7890", PhoneNumberCreator.createPhoneNumber(new int[]{1,2,3,4,5,6,7,8,9,0}));
    }

    @Test
    void testCreatePhoneNumber6() {
        assertEquals("(000) 000-0000", PhoneNumberCreator.createPhoneNumber(new int[]{0,0,0,0,0,0,0,0,0,0}));
    }

    //boundary cases
    @Test
    void testCreatePhoneNumber7() {
        assertEquals("(123) 456-7890", PhoneNumberCreator.createPhoneNumber(new int[]{1,2,3,4,5,6,7,8,9,0}));
    }

    @Test
    void testCreatePhoneNumber8() {
        assertEquals("(000) 000-0000", PhoneNumberCreator.createPhoneNumber(new int[]{0,0,0,0,0,0,0,0,0,0}));
    }

    @Test
    void testCreatePhoneNumber9() {
        assertEquals("(999) 999-9999", PhoneNumberCreator.createPhoneNumber(new int[]{9,9,9,9,9,9,9,9,9,9}));
    }

    @Test
    void testCreatePhoneNumber10() {
        assertEquals("(123) 456-7890", PhoneNumberCreator.createPhoneNumber(new int[]{1,2,3,4,5,6,7,8,9,0}));
    }
}
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.compile_result.status == CommandRunStatus.Finished


def test_junit_fail():
    request = RunCodeRequest(language='junit',
                             code='''
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

public class PhoneNumberCreator {
    public static String createPhoneNumber(int[] numbers) {
        StringBuilder phoneNumber = new StringBuilder("(");
        for (int i = 0; i < numbers.length; i++) {
            if (i == 9) {
                phoneNumber.append(") ");
            } else if (i == 2) {
                phoneNumber.append("-");
            }
            phoneNumber.append(numbers[i]);
        }
        return phoneNumber.toString();
    }
}

class PhoneNumberCreatorTest {
    @Test
    void testCreatePhoneNumber2() {
        assertEquals("(987) 654-3210", PhoneNumberCreator.createPhoneNumber(new int[]{9,8,7,6,5,4,3,2,1,0}));
    }

    @Test
    void testCreatePhoneNumber3() {
        assertEquals("(111) 111-1111", PhoneNumberCreator.createPhoneNumber(new int[]{1,1,1,1,1,1,1,1,1,1}));
    }

    @Test
    void testCreatePhoneNumber4() {
        assertEquals("(999) 999-9999", PhoneNumberCreator.createPhoneNumber(new int[]{9,9,9,9,9,9,9,9,9,9}));
    }

    @Test
    void testCreatePhoneNumber5() {
        assertEquals("(123) 456-7890", PhoneNumberCreator.createPhoneNumber(new int[]{1,2,3,4,5,6,7,8,9,0}));
    }

    @Test
    void testCreatePhoneNumber6() {
        assertEquals("(000) 000-0000", PhoneNumberCreator.createPhoneNumber(new int[]{0,0,0,0,0,0,0,0,0,0}));
    }

    //boundary cases
    @Test
    void testCreatePhoneNumber7() {
        assertEquals("(123) 456-7890", PhoneNumberCreator.createPhoneNumber(new int[]{1,2,3,4,5,6,7,8,9,0}));
    }

    @Test
    void testCreatePhoneNumber8() {
        assertEquals("(000) 000-0000", PhoneNumberCreator.createPhoneNumber(new int[]{0,0,0,0,0,0,0,0,0,0}));
    }

    @Test
    void testCreatePhoneNumber9() {
        assertEquals("(999) 999-9999", PhoneNumberCreator.createPhoneNumber(new int[]{9,9,9,9,9,9,9,9,9,9}));
    }

    @Test
    void testCreatePhoneNumber10() {
        assertEquals("(123) 456-7890", PhoneNumberCreator.createPhoneNumber(new int[]{1,2,3,4,5,6,7,8,9,0}));
    }
}
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.compile_result.status == CommandRunStatus.Finished
