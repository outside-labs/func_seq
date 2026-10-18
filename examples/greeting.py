"""Run with: func-seq run examples.greeting:script --checkpoint greeting.progress.json"""

from func_seq import Script


def collect():
    return {"message": "Hello from func_seq!"}


def display(message):
    print(message)
    return {"displayed": True}


script = Script("greeting", version="1")
collected = script.add_function(collect)
script.add_function(display, collected["message"])
