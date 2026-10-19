def add(a: float, b: float) -> float:
    return a + b


def calculate_average(numbers: list[float]) -> float:
    """Calculate the average of a list of numbers.
    BUG: Crashes with ZeroDivisionError when numbers list is empty!
    Should return 0.0 when numbers is empty.
    """
    if not numbers:
        return 0.0
    return sum(numbers) / len(numbers)
