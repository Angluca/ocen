Here are a list of other issues to fix, PLEASE UPDATE YOUR TODOS WITH THESE!!!

- Long lines should be wrapped
    - eg: imports, split after `::{` and put all the fields on a new line:
```
import @ast::nodes::{
    ImportPart, Import, Argument, MatchCond, MatchCondArg, MatchCase,
    Enum, EnumVariant, Specialization
}
```

- Spacing not preserved. If there are multiple (2+) explicit new lines between imports / comments / statements / struct fields etc, they should be preserved. 
    Currently it seems like all multiple new lines are just removed indiscriminately. This is not great for having code separation.

- Comments on consecutive lines should all be aligned

- If a single-line if statement has `return` or `break` or `continue`, we don't need `then`. Only use it when the statement doesn't start with a keyword.

- If function arguments / array elements / etc are broken up into new lines, we should have a trailing comma at the end. (Similar to how most languages do this)

- Empty blocks should be like `{}`, no new line. Currently you output `else => {\n}` instead of `else => {}`

