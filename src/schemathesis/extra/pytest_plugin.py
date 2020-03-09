from functools import partial
from typing import Any, Callable, Generator, List, Optional, Type, cast

import pytest
from _pytest import fixtures, nodes
from _pytest.config import hookimpl
from _pytest.fixtures import FuncFixtureInfo
from _pytest.python import Class, Function, FunctionDefinition, Metafunc, Module, PyCollector
from hypothesis.errors import InvalidArgument, Unsatisfiable  # pylint: disable=ungrouped-imports
from hypothesis.reporting import base_report

from .._hypothesis import create_test
from ..constants import InputType
from ..exceptions import InvalidSchema
from ..models import Endpoint
from ..utils import is_schemathesis_test, capture_hypothesis_output


class SchemathesisCase(PyCollector):
    def __init__(self, test_function: Callable, *args: Any, **kwargs: Any) -> None:
        self.test_function = test_function
        self.schemathesis_case = test_function._schemathesis_test  # type: ignore
        super().__init__(*args, **kwargs)

    def _get_test_name(self, endpoint: Endpoint, input_type: InputType) -> str:
        return f"{self.name}[{input_type.value}_input][{endpoint.method}:{endpoint.path}]"

    def _gen_items(self, endpoint: Endpoint, input_type: InputType) -> Generator[Function, None, None]:
        """Generate all items for the given endpoint.

        Could produce more than one test item if
        parametrization is applied via ``pytest.mark.parametrize`` or ``pytest_generate_tests``.

        This implementation is based on the original one in pytest, but with slight adjustments
        to produce tests out of hypothesis ones.
        """
        name = self._get_test_name(endpoint, input_type)
        funcobj = self._make_test(endpoint, input_type)

        cls = self._get_class_parent()
        definition = FunctionDefinition(name=self.name, parent=self.parent, callobj=funcobj)
        fixturemanager = self.session._fixturemanager
        fixtureinfo = fixturemanager.getfixtureinfo(definition, funcobj, cls)

        metafunc = self._parametrize(cls, definition, fixtureinfo)

        if not metafunc._calls:
            yield SchemathesisFunction(name, parent=self.parent, callobj=funcobj, fixtureinfo=fixtureinfo, `input_type=input_type)
        else:
            fixtures.add_funcarg_pseudo_fixture_def(self.parent, metafunc, fixturemanager)
            fixtureinfo.prune_dependency_tree()

            for callspec in metafunc._calls:
                subname = "{}[{}]".format(name, callspec.id)
                yield SchemathesisFunction(
                    name=subname,
                    parent=self.parent,
                    callspec=callspec,
                    callobj=funcobj,
                    fixtureinfo=fixtureinfo,
                    keywords={callspec.id: True},
                    originalname=name,
                    input_type=input_type,
                )

    def _get_class_parent(self) -> Optional[Type]:
        clscol = self.getparent(Class)
        return clscol.obj if clscol else None

    def _parametrize(
        self, cls: Optional[Type], definition: FunctionDefinition, fixtureinfo: FuncFixtureInfo
    ) -> Metafunc:
        module = self.getparent(Module).obj
        metafunc = Metafunc(definition, fixtureinfo, self.config, cls=cls, module=module)
        methods = []
        if hasattr(module, "pytest_generate_tests"):
            methods.append(module.pytest_generate_tests)
        if hasattr(cls, "pytest_generate_tests"):
            cls = cast(Type, cls)
            methods.append(cls().pytest_generate_tests)
        self.ihook.pytest_generate_tests.call_extra(methods, {"metafunc": metafunc})
        return metafunc

    def _make_test(self, endpoint: Endpoint, input_type: InputType) -> Callable:
        try:
            return create_test(endpoint, self.test_function, input_type=input_type)
        except InvalidSchema:
            return lambda: pytest.fail("Invalid schema for endpoint")

    def collect(self) -> List[Function]:  # type: ignore
        """Generate different test items for all endpoints available in the given schema."""
        try:
            return [
                item
                for input_type in self.schemathesis_case.input_types
                for endpoint in self.schemathesis_case.get_all_endpoints()
                for item in self._gen_items(endpoint, input_type)
            ]
        except Exception:
            pytest.fail("Error during collection")


class SchemathesisFunction(Function):  # pylint: disable=too-many-ancestors
    
    def __init__(self, *args, input_type, **kwargs):
        self.input_type = input_type
        super().__init__(*args, **kwargs)
    
    def _getobj(self) -> partial:
        """Tests defined as methods require `self` as the first argument.

        This method is called only for this case.
        """
        return partial(self.obj, self.parent.obj)


@hookimpl(hookwrapper=True)  # type:ignore # pragma: no mutate
def pytest_pycollect_makeitem(collector: nodes.Collector, name: str, obj: Any) -> None:
    """Switch to a different collector if the test is parametrized marked by schemathesis."""
    outcome = yield
    if is_schemathesis_test(obj):
        outcome.force_result(SchemathesisCase(obj, name, collector))
    else:
        outcome.get_result()


@hookimpl(hookwrapper=True)  # pragma: no mutate
def pytest_pyfunc_call(pyfuncitem):  # type:ignore
    """It is possible to have a Hypothesis exception in runtime.

    For example - kwargs validation is failed for some strategy.
    """
    with capture_hypothesis_output() as output:
        outcome = yield
    if isinstance(pyfuncitem, SchemathesisFunction) and pyfuncitem.input_type is InputType.invalid and outcome.excinfo and isinstance(outcome.excinfo[1], Unsatisfiable):
        pytest.skip("BAR")
    else:
        for text in output:
            base_report(text)
    try:
        outcome.get_result()
    except InvalidArgument as exc:
        pytest.fail(exc.args[0])
