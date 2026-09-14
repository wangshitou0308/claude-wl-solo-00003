"""求解器单元测试：确定性、歧义、不可行、截断保护。"""

from app.solver import Appearance, CellDemand, GroupInput, cell_index, solve_group

APP = Appearance("abc", "白", "圆")


def group(demands, residual=None, scattered=0, has_optional=False):
    return GroupInput(
        appearance=APP,
        med_ids=["A"],
        demands=demands,
        residual=residual or {},
        scattered=scattered,
        has_optional=has_optional,
    )


def test_fully_determined():
    g = group(
        demands={
            cell_index(1, "morning"): CellDemand(required=1),
            cell_index(2, "morning"): CellDemand(required=1),
        },
        residual={cell_index(1, "morning"): 1},
        scattered=1,
    )
    out = solve_group(g)
    assert out.feasible and not out.truncated
    assert out.solution_count == 1
    assert out.x_values[cell_index(1, "morning")] == [0]
    assert out.x_values[cell_index(2, "morning")] == [1]


def test_allowed_empty_cells_cause_ambiguity():
    g = group(
        demands={
            cell_index(1, "morning"): CellDemand(optional=[1]),
            cell_index(2, "morning"): CellDemand(optional=[1]),
        },
        scattered=1,
        has_optional=True,
    )
    out = solve_group(g)
    assert out.feasible
    assert out.solution_count == 2
    assert out.x_values[cell_index(1, "morning")] == [0, 1]
    assert out.x_values[cell_index(2, "morning")] == [0, 1]


def test_missing_pills_infeasible():
    g = group(demands={cell_index(1, "morning"): CellDemand(required=1)})
    out = solve_group(g)
    assert not out.feasible
    assert out.min_total == 1 and out.max_total == 1


def test_residual_exceeds_capacity_infeasible():
    g = group(
        demands={cell_index(1, "morning"): CellDemand(required=1)},
        residual={cell_index(1, "morning"): 2},
    )
    assert not solve_group(g).feasible


def test_residual_in_unscheduled_cell_infeasible():
    g = group(demands={}, residual={cell_index(4, "noon"): 1})
    assert not solve_group(g).feasible


def test_surplus_pills_infeasible():
    g = group(
        demands={cell_index(1, "morning"): CellDemand(required=1)},
        residual={cell_index(1, "morning"): 1},
        scattered=1,
    )
    assert not solve_group(g).feasible


def test_identical_appearance_two_meds_still_determined():
    """两种药外观相同但计划固定时，散落入格数量仍可唯一确定。"""
    g = GroupInput(
        appearance=APP,
        med_ids=["A", "B"],
        demands={
            cell_index(1, "morning"): CellDemand(required=1),  # A
            cell_index(2, "morning"): CellDemand(required=2),  # B
        },
        residual={cell_index(1, "morning"): 1},
        scattered=2,
    )
    out = solve_group(g)
    assert out.feasible and out.solution_count == 1
    assert out.x_values[cell_index(2, "morning")] == [2]


def test_truncation_protection():
    demands = {cell_index(d, "morning"): CellDemand(optional=[1])
               for d in range(1, 8)}
    demands.update({cell_index(d, "noon"): CellDemand(optional=[1])
                    for d in range(1, 5)})
    g = group(demands=demands, scattered=5, has_optional=True)
    out = solve_group(g, max_solutions=10)
    assert out.truncated
    assert out.solution_count is None
