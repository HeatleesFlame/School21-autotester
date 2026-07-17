import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# ===================== CONFIG =====================

DEFAULT_BRANCH = "develop"

C_EXTENSIONS = {".c"}
CPP_EXTENSIONS = {".cc", ".cpp", ".cxx"}

CLANG_FORMAT = "clang-format"
REQUIRED_CLANG_FORMAT_VERSION = "18.1.8"

CPPCHECK = "cppcheck"
VALGRIND = "valgrind"

TIMEOUT_SECONDS = 5
VALGRIND_ERROR_CODE = 37

# False — игнорировать пробелы и переводы строк по краям stdout.
# True — сравнивать вывод строго символ в символ.
STRICT_OUTPUT = False

# Корень для временной директории по умолчанию.
# Внимание: snap-версии clang-format/cppcheck/valgrind не имеют доступа
# к /tmp из-за конфайнмента. В этом случае используйте --workdir-root ~
# (или любой путь в домашней директории).
DEFAULT_WORKDIR_ROOT = "/tmp"

# Регэксп для режима --test-map prefix.
# Берёт ведущий токен вида буквы+цифры из имени файла:
#   D07_matrix.c   -> D07
#   D08_stack.cpp  -> D08
#   d12task.c      -> d12
PREFIX_PATTERN = re.compile(r"^([A-Za-z]+\d+)")

VALGRIND_FLAGS = [
    "--tool=memcheck",
    "--leak-check=full",
    "--show-leak-kinds=all",
    "--errors-for-leak-kinds=all",
    "--track-origins=yes",
    f"--error-exitcode={VALGRIND_ERROR_CODE}",
]

# ==================================================


def normalize_output(text: str) -> str:
    if STRICT_OUTPUT:
        return text
    return text.strip()


def short(text: str, limit: int = 3000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... truncated ..."


def tool_exists(name: str) -> bool:
    return shutil.which(name) is not None


def day_from_repo_url(repo_url: str) -> str:
    # День = первые три символа имени репозитория.
    #   .../D08T05_ID_1577486-1.git -> D08T05_ID_1577486-1 -> D08
    name = repo_url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-len(".git")]
    return name[:3]


def run_cmd(cmd, cwd=None, input_data=None, timeout=None):
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -999, "", "TIMEOUT"


def require_tools():
    tools = [
        "git",
        "gcc",
        "g++",
        CLANG_FORMAT,
        CPPCHECK,
        VALGRIND,
    ]

    missing = [tool for tool in tools if not tool_exists(tool)]

    if missing:
        print("Missing tools:")
        for tool in missing:
            print(f"  - {tool}")
        sys.exit(1)


def check_clang_format_version():
    code, out, err = run_cmd([CLANG_FORMAT, "--version"])

    if code != 0:
        print("Cannot get clang-format version")
        print(short(out))
        print(short(err))
        sys.exit(1)

    version_text = out + err

    if REQUIRED_CLANG_FORMAT_VERSION not in version_text:
        print("Wrong clang-format version")
        print(f"Required: {REQUIRED_CLANG_FORMAT_VERSION}")
        print(f"Actual:   {version_text.strip()}")
        sys.exit(1)


def clone_repo_to_tmp(repo_url: str, keep_workdir: bool, workdir_root: str):
    root = Path(workdir_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    if keep_workdir:
        workdir = Path(
            tempfile.mkdtemp(
                prefix="c_autotest_",
                dir=str(root),
            )
        )
        temp_dir_holder = None
    else:
        temp_dir_holder = tempfile.TemporaryDirectory(
            prefix="c_autotest_",
            dir=str(root),
        )
        workdir = Path(temp_dir_holder.name)

    print("Cloning repository:")
    print(f"  repo:    {repo_url}")
    print(f"  workdir: {workdir}")

    code, out, err = run_cmd(["git", "clone", repo_url, str(workdir)])

    if code != 0:
        print("git clone failed")
        print(short(out))
        print(short(err))
        if temp_dir_holder is not None:
            temp_dir_holder.cleanup()
        sys.exit(1)

    return workdir, temp_dir_holder


def checkout_branch(project_dir: Path, branch: str):
    print(f"Checkout branch: {branch}")

    code, out, err = run_cmd(
        ["git", "checkout", branch],
        cwd=project_dir,
    )

    if code != 0:
        print(f"git checkout {branch} failed")
        print(short(out))
        print(short(err))
        sys.exit(1)



def prepare_clang_format(project_dir: Path, scan_dir: str):
    source_dir = project_dir / scan_dir

    if not source_dir.exists():
        print(f"Scan directory not found: {source_dir}")
        sys.exit(1)

    # .clang-format всегда берётся из проекта студента.
    style_file = project_dir / "materials" / "linters" / ".clang-format"

    if not style_file.exists():
        print(f"clang-format config not found: {style_file}")
        sys.exit(1)

    target_style = source_dir / ".clang-format"
    shutil.copyfile(style_file, target_style)

    print("Copied clang-format config:")
    print(f"  {style_file}")
    print(f"  -> {target_style}")


def scan_sources(project_dir: Path, scan_dir: str, recursive: bool):
    base_dir = project_dir / scan_dir

    if not base_dir.exists():
        print(f"Scan directory not found: {scan_dir}")
        sys.exit(1)

    pattern = "**/*" if recursive else "*"
    sources = []

    for path in base_dir.glob(pattern):
        if not path.is_file():
            continue

        suffix = path.suffix.lower()

        if suffix in C_EXTENSIONS or suffix in CPP_EXTENSIONS:
            relative = path.relative_to(project_dir)
            sources.append(relative.as_posix())

    sources.sort()
    return sources


def parse_selection(value: str):
    # "det, invert , sort" -> {"det", "invert", "sort"}
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def filter_sources(sources, only, skip):
    # Отбор по имени файла без расширения (stem), совпадает с папкой тестов.
    result = sources

    if only:
        result = [s for s in result if Path(s).stem in only]

    if skip:
        result = [s for s in result if Path(s).stem not in skip]

    return result


def print_found_sources(sources):
    print()
    print("Found source files:")

    if not sources:
        print("  No source files found")
        return

    for source in sources:
        print(f"  - {source}")


def is_c_file(source_file: str) -> bool:
    return Path(source_file).suffix.lower() in C_EXTENSIONS


def is_cpp_file(source_file: str) -> bool:
    return Path(source_file).suffix.lower() in CPP_EXTENSIONS


def check_format(project_dir: Path, source_file: str):
    cmd = [
        CLANG_FORMAT,
        "-n",
        "--Werror",
        source_file,
    ]

    code, out, err = run_cmd(cmd, cwd=project_dir)

    if code == 0:
        return True, ""

    msg = "clang-format failed"
    if out:
        msg += "\n" + short(out)
    if err:
        msg += "\n" + short(err)

    return False, msg


def run_cppcheck(project_dir: Path, source_file: str):
    if is_c_file(source_file):
        language = "c"
        standard = "c11"
    else:
        language = "c++"
        standard = "c++17"

    cmd = [
        CPPCHECK,
        f"--language={language}",
        f"--std={standard}",
        "--enable=warning,style,performance,portability",
        "--error-exitcode=1",
        "--suppress=missingIncludeSystem",
        source_file,
    ]

    code, out, err = run_cmd(cmd, cwd=project_dir)

    if code == 0:
        return True, ""

    msg = "cppcheck failed"
    if out:
        msg += "\n" + short(out)
    if err:
        msg += "\n" + short(err)

    return False, msg


def compile_program(project_dir: Path, source_file: str, build_dir: Path):
    safe_name = (
        source_file
        .replace("/", "_")
        .replace("\\", "_")
        .replace(".", "_")
    )

    exe_path = build_dir / safe_name

    if is_c_file(source_file):
        compiler = "gcc"
        flags = [
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-g",
        ]
    else:
        compiler = "g++"
        flags = [
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-g",
        ]

    # -lm нужен для программ, использующих math.h (sqrt, pow и т.п.).
    # Линкер требует его явно и должен идти после исходника.
    cmd = [
        compiler,
        source_file,
        "-o",
        str(exe_path),
        *flags,
        "-lm",
    ]

    code, out, err = run_cmd(cmd, cwd=project_dir)

    if code == 0:
        return True, exe_path, ""

    msg = "compile failed"
    if out:
        msg += "\n" + short(out)
    if err:
        msg += "\n" + short(err)

    return False, None, msg


def tests_dir_for_source(source_file: str, tests_root: Path, mode: str):
    source_path = Path(source_file)

    if mode == "stem":
        # src/task1.c -> tests/task1
        return tests_root / source_path.stem

    if mode == "relative":
        # src/lab1/task1.c -> tests/src_lab1_task1
        without_suffix = source_path.with_suffix("").as_posix()
        safe_name = without_suffix.replace("/", "_").replace("\\", "_")
        return tests_root / safe_name

    if mode == "prefix":
        # src/D07_matrix.c -> tests/D07
        # src/D08_stack.cpp -> tests/D08
        match = PREFIX_PATTERN.match(source_path.stem)
        if match is None:
            return None
        return tests_root / match.group(1)

    if mode == "prefix-nested":
        # src/D07_matrix.c -> tests/D07/D07_matrix
        # src/D08_stack.cpp -> tests/D08/D08_stack
        match = PREFIX_PATTERN.match(source_path.stem)
        if match is None:
            return None
        return tests_root / match.group(1) / source_path.stem

    raise ValueError(f"Unknown test map mode: {mode}")


def get_test_pairs(tests_dir: Path):
    if not tests_dir.exists():
        return None, f"tests directory not found: {tests_dir}"

    input_files = sorted(tests_dir.glob("*.in"))

    if not input_files:
        return None, f"no .in files found in: {tests_dir}"

    pairs = []

    for input_file in input_files:
        expected_file = input_file.with_suffix(".out")

        if not expected_file.exists():
            return None, f"missing expected output file: {expected_file}"

        pairs.append((input_file, expected_file))

    return pairs, ""


def run_program_with_valgrind(project_dir: Path, exe_path: Path, input_data: str):
    cmd = [
        VALGRIND,
        *VALGRIND_FLAGS,
        str(exe_path),
    ]

    return run_cmd(
        cmd,
        cwd=project_dir,
        input_data=input_data,
        timeout=TIMEOUT_SECONDS,
    )


def run_tests(project_dir: Path, exe_path: Path, tests_dir: Path):
    pairs, error = get_test_pairs(tests_dir)

    if error:
        return {
            "output_ok": False,
            "memory_ok": False,
            "passed_output": 0,
            "passed_memory": 0,
            "total": 0,
            "messages": [error],
        }

    total = len(pairs)
    passed_output = 0
    passed_memory = 0
    messages = []

    for input_file, expected_file in pairs:
        test_name = input_file.name

        input_data = input_file.read_text(encoding="utf-8")
        expected = expected_file.read_text(encoding="utf-8")

        code, out, err = run_program_with_valgrind(
            project_dir,
            exe_path,
            input_data,
        )

        if code == -999:
            messages.append(f"{test_name}: TIMEOUT")
            continue

        output_is_ok = normalize_output(out) == normalize_output(expected)

        if output_is_ok:
            passed_output += 1
        else:
            messages.append(
                f"{test_name}: wrong answer\n"
                f"expected:\n{short(expected)}\n\n"
                f"actual:\n{short(out)}"
            )

        if code == 0:
            passed_memory += 1
        elif code == VALGRIND_ERROR_CODE:
            messages.append(
                f"{test_name}: valgrind memory error\n"
                f"{short(err)}"
            )
        else:
            messages.append(
                f"{test_name}: runtime error, exit code {code}\n"
                f"{short(err)}"
            )

    return {
        "output_ok": passed_output == total,
        "memory_ok": passed_memory == total,
        "passed_output": passed_output,
        "passed_memory": passed_memory,
        "total": total,
        "messages": messages,
    }


def check_program(
    project_dir: Path,
    source_file: str,
    build_dir: Path,
    tests_root: Path,
    test_map_mode: str,
):
    result = {
        "file": source_file,
        "format_ok": None,
        "cppcheck_ok": None,
        "compile_ok": None,
        "output_ok": None,
        "memory_ok": None,
        "output_passed": 0,
        "memory_passed": 0,
        "tests_total": 0,
        "tests_dir": None,
        "messages": [],
    }

    ok, msg = check_format(project_dir, source_file)
    result["format_ok"] = ok
    if not ok:
        result["messages"].append(msg)

    ok, msg = run_cppcheck(project_dir, source_file)
    result["cppcheck_ok"] = ok
    if not ok:
        result["messages"].append(msg)

    ok, exe_path, msg = compile_program(project_dir, source_file, build_dir)
    result["compile_ok"] = ok
    if not ok:
        result["messages"].append(msg)
        return result

    tests_dir = tests_dir_for_source(source_file, tests_root, test_map_mode)

    if tests_dir is None:
        result["output_ok"] = False
        result["memory_ok"] = False
        result["messages"].append(
            f"cannot determine tests directory for {source_file} "
            f"in test-map mode '{test_map_mode}'"
        )
        return result

    result["tests_dir"] = str(tests_dir)

    test_result = run_tests(project_dir, exe_path, tests_dir)

    result["output_ok"] = test_result["output_ok"]
    result["memory_ok"] = test_result["memory_ok"]
    result["output_passed"] = test_result["passed_output"]
    result["memory_passed"] = test_result["passed_memory"]
    result["tests_total"] = test_result["total"]
    result["messages"].extend(test_result["messages"])

    return result


def result_verdict(result: dict):
    checks = [
        result["format_ok"],
        result["cppcheck_ok"],
        result["compile_ok"],
        result["output_ok"],
        result["memory_ok"],
    ]

    if all(checks):
        return "OK"

    return "FAIL"


def print_report(results):
    print()
    print("=" * 80)
    print("REPORT")
    print("=" * 80)

    for result in results:
        verdict = result_verdict(result)

        print()
        print(f"File: {result['file']}")
        print(f"Verdict: {verdict}")

        print(f"  format:      {'OK' if result['format_ok'] else 'FAIL'}")
        print(f"  cppcheck:    {'OK' if result['cppcheck_ok'] else 'FAIL'}")
        print(f"  compile:     {'OK' if result['compile_ok'] else 'FAIL'}")

        if result["tests_dir"]:
            print(f"  tests dir:   {result['tests_dir']}")

        if result["output_ok"] is None:
            print("  output:      SKIPPED")
        else:
            print(
                f"  output:      "
                f"{'OK' if result['output_ok'] else 'FAIL'} "
                f"({result['output_passed']}/{result['tests_total']})"
            )

        if result["memory_ok"] is None:
            print("  memory:      SKIPPED")
        else:
            print(
                f"  memory:      "
                f"{'OK' if result['memory_ok'] else 'FAIL'} "
                f"({result['memory_passed']}/{result['tests_total']})"
            )

        if result["messages"]:
            print("  messages:")
            for msg in result["messages"]:
                for line in short(msg).splitlines():
                    print(f"    {line}")

    print()
    final_ok = bool(results) and all(result_verdict(result) == "OK" for result in results)

    if final_ok:
        print("FINAL VERDICT: OK")
    else:
        print("FINAL VERDICT: FAIL")

    return final_ok


def main():
    parser = argparse.ArgumentParser(
        description="Autotest runner for C/C++ student projects"
    )

    parser.add_argument(
        "repo",
        help="student repository URL",
    )

    parser.add_argument(
        "--branch",
        default=DEFAULT_BRANCH,
        help=f"branch to checkout, default: {DEFAULT_BRANCH}",
    )

    parser.add_argument(
        "--scan-dir",
        default="src",
        help="directory inside student repository to scan, default: src",
    )

    parser.add_argument(
        "--tests-root",
        default=None,
        help="directory with tests; default: ./tests inside tester repository",
    )

    parser.add_argument(
        "--recursive",
        action="store_true",
        help="scan source directory recursively",
    )

    parser.add_argument(
        "--only",
        default=None,
        help=(
            "comma-separated list of quests to check, by file stem, "
            "e.g. --only det,invert. Others are skipped"
        ),
    )

    parser.add_argument(
        "--skip",
        default=None,
        help=(
            "comma-separated list of quests to skip, by file stem, "
            "e.g. --skip picture,sort"
        ),
    )

    parser.add_argument(
        "--test-map",
        choices=["stem", "relative", "prefix", "prefix-nested"],
        default="stem",
        help=(
            "how to map source file to tests directory: "
            "stem: src/task1.c -> tests/task1, "
            "relative: src/task1.c -> tests/src_task1, "
            "prefix: src/D07_matrix.c -> tests/D07, "
            "prefix-nested: src/D07_matrix.c -> tests/D07/D07_matrix"
        ),
    )

    parser.add_argument(
        "--workdir-root",
        default=DEFAULT_WORKDIR_ROOT,
        help=(
            "root directory for the temporary clone, default: /tmp. "
            "Use a path in your home dir (e.g. ~) if clang-format/cppcheck/"
            "valgrind are installed via snap and cannot access /tmp"
        ),
    )

    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="do not delete cloned repository after testing",
    )

    parser.add_argument(
        "--no-checkout",
        action="store_true",
        help="do not switch repository to branch",
    )

    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent

    if args.tests_root is None:
        tests_root = script_dir / "tests"
    else:
        tests_root = Path(args.tests_root).resolve()

    # День берётся из имени репозитория (первые три символа).
    # Папка дня добавляется в путь к тестам: tests/<day>/<stem>
    day = day_from_repo_url(args.repo)
    tests_root = tests_root / day
    print(f"Day: {day}")

    if not tests_root.exists():
        print(f"Tests directory not found: {tests_root}")
        sys.exit(1)





    require_tools()
    check_clang_format_version()

    project_dir, temp_dir_holder = clone_repo_to_tmp(
        repo_url=args.repo,
        keep_workdir=args.keep_workdir,
        workdir_root=args.workdir_root,
    )

    try:
        if not args.no_checkout:
            checkout_branch(project_dir, args.branch)

        prepare_clang_format(
            project_dir=project_dir,
            scan_dir=args.scan_dir,

        )

        sources = scan_sources(
            project_dir=project_dir,
            scan_dir=args.scan_dir,
            recursive=args.recursive,
        )

        only = parse_selection(args.only)
        skip = parse_selection(args.skip)

        if only or skip:
            available = {Path(s).stem for s in sources}
            unknown = (only | skip) - available
            if unknown:
                print()
                print("Warning: selection names not found among sources:")
                for name in sorted(unknown):
                    print(f"  - {name}")

            sources = filter_sources(sources, only, skip)

        print_found_sources(sources)

        if not sources:
            print()
            print("No source files to check after selection")
            print("FINAL VERDICT: FAIL")
            sys.exit(1)

        build_dir = project_dir / "_build"
        build_dir.mkdir(exist_ok=True)

        results = []

        for source_file in sources:
            print()
            print("=" * 80)
            print(f"Checking {source_file}")
            print("=" * 80)

            result = check_program(
                project_dir=project_dir,
                source_file=source_file,
                build_dir=build_dir,
                tests_root=tests_root,
                test_map_mode=args.test_map,
            )

            results.append(result)

        ok = print_report(results)

        if not ok:
            sys.exit(1)

    finally:
        if args.keep_workdir:
            print()
            print(f"Workdir was kept: {project_dir}")
        else:
            if temp_dir_holder is not None:
                temp_dir_holder.cleanup()


if __name__ == "__main__":
    main()