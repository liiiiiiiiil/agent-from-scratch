"""calculate 工具：计算数学表达式。"""

import re

from mini_agent.tools.base import Tool


def calculate(expression: str):
    """计算一个数学表达式。"""
    if not re.fullmatch(r"[\d\s+\-*/().]+", expression):
        raise ValueError("表达式包含非法字符")

    return str(eval(expression))


calculate_tool = Tool(
    name="calculate",
    description="计算一个数学表达式",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "数学表达式，例如 3+5*2",
            }
        },
        "required": ["expression"],
    },
    handler=calculate,
    delegation_capability="pure_compute",
)
