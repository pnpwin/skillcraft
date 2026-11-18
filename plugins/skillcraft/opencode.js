import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { Plugin, Skill } from '@opencode/plugin';
import { parse } from 'yaml';

const skillID = Skill.ID.make('skillcraft');
const skillPath = fileURLToPath(new URL('./skills/skillcraft/SKILL.md', import.meta.url));

export default Plugin.define({
  id: 'skillcraft',
  async setup(ctx) {
    const source = await readFile(skillPath, 'utf8');
    const frontmatter = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(source);
    if (!frontmatter) throw Error('SkillCraft is missing YAML frontmatter');
    const metadata = parse(frontmatter[1]);
    if (metadata?.name !== 'skillcraft' || typeof metadata.description !== 'string' || !metadata.description.trim()) {
      throw Error('SkillCraft frontmatter must have its package name and a description');
    }
    await ctx.skill.transform((editor) => {
      // Preserve a standalone skill already discovered in this location.
      if (editor.get(skillID)) return;
      editor.add({
        id: skillID,
        name: Skill.Name.make(metadata.name),
        description: metadata.description,
        path: Skill.Info.fields.path.make(skillPath),
        content: source.slice(frontmatter[0].length),
      });
    });
  },
});
