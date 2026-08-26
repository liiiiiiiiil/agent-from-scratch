import sys
from mini_agent.agent import agent_loop

if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    messages = [{"role": "system", "content": "你是一个助手，通过调用工具完成任务。"}]
    if len(sys.argv) > 1:
        messages.append({"role": "user", "content": sys.argv[1]})
        reply = agent_loop(messages)
        print(reply)

    while True:
        try:
            user_input = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input or user_input.lower() in ("exit", "quit"):
            break
        messages.append({"role": "user", "content": user_input})
        reply = agent_loop(messages)
        print(reply)
