// Disposable browser entry: bundle the implementation under test, not a copied query builder.
import { ggufVariantsQuery } from "../../studio/frontend/src/features/chat/api/gguf-variants-request";
import { loadPickerGgufVariants } from "../../studio/frontend/src/features/model-picker/components/model-selector/gguf-discovery";
import { verifiedSoleHubVariant } from "../../studio/frontend/src/features/model-picker/components/model-selector/sole-quant-cache";

Object.assign(window, {
  issue12415: { ggufVariantsQuery, loadPickerGgufVariants, verifiedSoleHubVariant },
});
