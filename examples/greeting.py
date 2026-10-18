"""Run with: func-seq run examples.greeting:sequence --checkpoint greeting.progress.json"""

from func_seq import Sequence


def collect():
    return {"message": "Hello from func_seq!"}


def display(message):
    print(message)
    return {"displayed": True}


sequence = Sequence("greeting", version="1")
collected = sequence.add_function(collect)
sequence.add_function(display, collected["message"])
