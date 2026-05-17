import Lake
open Lake DSL

package «sandbox» where
  -- add package configuration options here
  leanOptions := #[
    ⟨`pp.unicode.fun, true⟩, -- pretty-prints `fun a ↦ b`
    ⟨`pp.proofs.withType, false⟩
  ]

require mathlib from git
  "https://github.com/leanprover-community/mathlib4.git"

lean_lib «Sandbox» where
  -- add library configuration options here

@[default_target]
lean_exe «sandbox» where
  root := `Main
