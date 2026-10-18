"""Run with: run-script run examples.greeting:script --checkpoint greeting.progress.json"""

from run_script import Script


def collect():
    return {"message": "Hello from run_script!"}


def display(message):
    print(message)
    return {"displayed": True}


script = Script("greeting", version="1")
collected = script.add_function(collect)
script.add_function(display, collected["message"])
