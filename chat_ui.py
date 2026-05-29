import asyncio
import os
from lumen_sre.agent import run_sre_query

async def main():
    os.system('clear')
    print("="*65)
    print("      LUMEN SRE AUTOMATION AGENT - INTERACTIVE TEST PORTAL")
    print("="*65)
    print("  Type your infrastructure queries below.")
    print("  Type 'exit' or 'quit' to close the terminal session.\n")
    
    while True:
        user_query = input("\033[1;36mYou:\033[0m ")
        if user_query.strip().lower() in ['exit', 'quit']:
            print("\nClosing SRE Automation Agent session. Goodbye!")
            break
        if not user_query.strip():
            continue
            
        print("\n\033[1;33mAgent is analyzing infrastructure & checking logs...\033[0m")
        try:
            response = await run_sre_query(user_query)
            print(f"\n\033[1;32mAgent:\033[0m {response}\n")
            print("-" * 65)
        except Exception as e:
            print(f"\n\033[1;31mError:\033[0m {str(e)}\n")

if __name__ == "__main__":
    asyncio.run(main())
