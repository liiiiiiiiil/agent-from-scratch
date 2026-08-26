from mini_agent.tools.base import Tool


def read_file(path: str):
    """
    真正执行读取文件的函数。
    """
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path: str, content: str):
    """
    真正执行写文件的函数。
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    return f"已写入文件: {path}"


read_file_tool = Tool(
    name="read_file",

    description="读取一个文本文件的内容",

    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件路径",
            }
        },
        "required": ["path"],
    },

    handler=read_file,
)


write_file_tool = Tool(
    name="write_file",

    description="将内容写入文本文件",

    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要写入的文件路径",
            },
            "content": {
                "type": "string",
                "description": "要写入的内容",
            },
        },
        "required": ["path", "content"],
    },

    handler=write_file,
)
