from mini_agent.tools.base import Tool


def calculate(expression: str):
    """
    真正执行计算的函数。
    """
    # 简单限制，只允许数字和数学运算符
    import re

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
)
