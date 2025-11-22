This is a repository with my custom programming language `ocen`. It is roughly similar to C in terms of the programming model, but with some syntax borrowed from rust/etc. It is not a production-ready language, but it is a fun project to work on.

There is an incomplete description of the language in docs/getting_started.md, and there are newer features such as closures / value enums / etc that are implemented and used but not documented. If ever needed, there are examples of all the available features in the tests/ directory (with negative tests in tests/bad directory).

Note that this is NOT rust despite similarities, and code should be written in a way that is similar to C, but using some of the modern features (ie: proper namespaces, methods, etc). There are many examples of the language actually being used in the tests/, examples/, compiler/ and std/ directories.

You should IGNORE the `bootstrap/` directory completely - this just contains build artifacts.