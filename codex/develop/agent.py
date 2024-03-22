import asyncio
import logging
import os

from prisma.enums import FunctionState
from prisma.models import (
    CompiledRoute,
    CompletedApp,
    Function,
    ObjectType,
    Specification,
)
from prisma.types import CompiledRouteCreateInput

from codex.api_model import Identifiers
from codex.common.database import INCLUDE_FUNC
from codex.common.model import FunctionDef
from codex.develop.compile import (
    compile_route,
    create_app,
    get_object_field_deps,
    get_object_type_deps,
)
from codex.develop.database import get_compiled_route
from codex.develop.develop import DevelopAIBlock
from codex.develop.function import (
    construct_function,
    generate_object_template,
    get_database_schema,
)

RECURSION_DEPTH_LIMIT = int(os.environ.get("RECURSION_DEPTH_LIMIT", 2))

logger = logging.getLogger(__name__)


async def process_api_route(api_route, ids, spec, app):
    if not api_route.RequestObject:
        types = []
        descs = {}
    elif api_route.RequestObject.Fields:
        # Unwrap the first level of the fields if it's a nested object.
        types = [(f.name, f.typeName) for f in api_route.RequestObject.Fields]
        descs = {f.name: f.description or "" for f in api_route.RequestObject.Fields}
    else:
        types = [("request", api_route.RequestObject.name)]
        descs = {"request": api_route.RequestObject.description or ""}

    function_def = FunctionDef(
        name=api_route.functionName,
        arg_types=types,
        arg_descs=descs,
        return_type=api_route.ResponseObject.name if api_route.ResponseObject else None,
        return_desc=api_route.ResponseObject.description or ""
        if api_route.ResponseObject
        else None,
        is_implemented=False,
        function_desc=api_route.description,
        function_code="",
    )
    object_type_ids = set()
    available_types = {
        dep_type.name: dep_type
        for obj in [api_route.RequestObject, api_route.ResponseObject]
        if obj
        for dep_type in await get_object_type_deps(obj.id, object_type_ids)
    }
    if api_route.RequestObject and api_route.RequestObject.Fields:
        # RequestObject is unwrapped, so remove it from the available types
        available_types.pop(api_route.RequestObject.name, None)
    compiled_route = await CompiledRoute.prisma().create(
        data=CompiledRouteCreateInput(
            description=api_route.description,
            fileName=api_route.functionName + "_service.py",
            mainFunctionName=api_route.functionName,
            compiledCode="",  # This will be updated by compile_route
            RootFunction={
                "create": await construct_function(function_def, available_types)
            },
            CompletedApp={"connect": {"id": app.id}},
            ApiRouteSpec={"connect": {"id": api_route.id}},
        ),
        include={"RootFunction": INCLUDE_FUNC},  # type: ignore
    )
    if not compiled_route.RootFunction:
        raise ValueError("Root function not created")

    # Set all the ids for logging
    ids.spec_id = spec.id
    ids.compiled_route_id = compiled_route.id
    ids.function_id = compiled_route.RootFunction.id

    route_root_func = await develop_route(
        ids=ids,
        compiled_route_id=compiled_route.id,
        goal_description=spec.context,
        function=compiled_route.RootFunction,
    )
    logger.info(f"Route function id: {route_root_func.id}")
    await compile_route(compiled_route.id, route_root_func)


async def develop_application(ids: Identifiers, spec: Specification) -> CompletedApp:
    """
    Develops an application based on the given identifiers and specification.

    Args:
        ids (Identifiers): The identifiers used for development.
        spec (Specification): The specification of the application.

    Returns:
        CompletedApp: The completed application.

    """
    app = await create_app(ids, spec, [])
    tasks = []

    if spec.ApiRouteSpecs:
        for api_route in spec.ApiRouteSpecs:
            # Schedule each API route for processing
            task = process_api_route(api_route, ids, spec, app)
            tasks.append(task)

        # Run the tasks concurrently
        await asyncio.gather(*tasks)

    return app


async def develop_route(
    ids: Identifiers,
    compiled_route_id: str,
    goal_description: str,
    function: Function,
    depth: int = 0,
) -> Function:
    """
    Recursively develops a function and its child functions
    based on the provided function definition and api route spec.

    Args:
        ids (Identifiers): The identifiers for the function.
        compiled_route_id (str): The id of the compiled route.
        goal_description (str): The high-level goal of the function to create.
        function (Function): The function to develop.
        depth (int): The depth of the recursion.

    Returns:
        Function: The developed route function.
    """
    logger.info(
        f"Developing for compiled route: "
        f"{compiled_route_id} - Func: {function.functionName}, depth: {depth}"
    )

    # Set the function id for logging
    ids.function_id = function.id
    ids.compiled_route_id = compiled_route_id

    if depth > RECURSION_DEPTH_LIMIT:
        raise ValueError("Recursion depth exceeded")

    compiled_route = await get_compiled_route(compiled_route_id)
    functions = []
    generated_func: dict[str, Function] = {}
    generated_objs: dict[str, ObjectType] = {}
    if compiled_route.Functions:
        functions += compiled_route.Functions
    if compiled_route.RootFunction:
        functions.append(compiled_route.RootFunction)

    object_ids = set()
    for func in functions:
        generated_func[func.functionName] = func

        # Populate generated request objects from the LLM
        for arg in func.FunctionArgs:
            for type in await get_object_field_deps(arg, object_ids):
                generated_objs[type.name] = type

        # Populate generated response objects from the LLM
        if func.FunctionReturn:
            for type in await get_object_field_deps(func.FunctionReturn, object_ids):
                generated_objs[type.name] = type

    provided_functions = [
        func.template
        for func in generated_func.values()
        if func.functionName != function.functionName
    ] + [generate_object_template(f) for f in generated_objs.values()]

    database_schema = get_database_schema(compiled_route)

    dev_invoke_params = {
        "database_schema": database_schema,
        "function_name": function.functionName,
        "goal": goal_description,
        "function_signature": function.template,
        # function_args is not used by the prompt, but used for function validation
        "function_args": [(f.name, f.typeName) for f in function.FunctionArgs or []],
        # function_rets is not used by the prompt, but used for function validation
        "function_rets": function.FunctionReturn.typeName
        if function.FunctionReturn
        else None,
        "provided_functions": provided_functions,
        # compiled_route_id is not used by the prompt, but is used by the function
        "compiled_route_id": compiled_route_id,
        # available_objects is not used by the prompt, but is used by the function
        "available_objects": generated_objs,
        # function_id is used, so we can update the function with the implementation
        "function_id": function.id,
        "available_functions": generated_func,
        "allow_stub": depth < RECURSION_DEPTH_LIMIT,
    }

    route_function = await DevelopAIBlock().invoke(
        ids=ids,
        invoke_params=dev_invoke_params,
    )

    if route_function.ChildFunctions:
        logger.info(
            f"\tDeveloping {len(route_function.ChildFunctions)} child functions"
        )
        tasks = [
            develop_route(
                ids=ids,
                compiled_route_id=compiled_route_id,
                goal_description=goal_description,
                function=child,
                depth=depth + 1,
            )
            for child in route_function.ChildFunctions
            if child.state == FunctionState.DEFINITION
        ]
        await asyncio.gather(*tasks)
    else:
        logger.info("📦 No child functions to develop")
        logger.debug(route_function.rawCode)
    return route_function


if __name__ == "__main__":
    import prisma

    import codex.common.test_const as test_consts
    from codex.common.ai_model import OpenAIChatClient
    from codex.common.logging_config import setup_logging
    from codex.requirements.database import get_latest_specification

    OpenAIChatClient.configure({})
    setup_logging(local=True)
    client = prisma.Prisma(auto_register=True)

    async def run_me():
        await client.connect()
        ids = test_consts.identifier_1
        assert ids.user_id
        assert ids.app_id
        spec = await get_latest_specification(ids.user_id, ids.app_id)
        ans = await develop_application(ids=ids, spec=spec)
        await client.disconnect()
        return ans

    ans = asyncio.run(run_me())
    logger.info(ans)
