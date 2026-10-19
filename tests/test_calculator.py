import unittest
from src.calculator import add, calculate_average


class TestCalculator(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 2), 3)

    def test_calculate_average(self):
        self.assertEqual(calculate_average([10, 20, 30]), 20.0)

    def test_calculate_average_empty(self):
        # Should return 0.0 for empty list without raising ZeroDivisionError
        self.assertEqual(calculate_average([]), 0.0)


if __name__ == "__main__":
    unittest.main()
